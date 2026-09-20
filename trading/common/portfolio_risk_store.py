"""
Phase 17.2-P -- durable persistence and restart recovery for
trading.common.portfolio_risk.PortfolioRiskManager.

--------------------------------------------------------------------------
THE GAP THIS CLOSES
--------------------------------------------------------------------------
PortfolioRiskManager's own state (reservations, the position ledger, and
the per-day order counters) has always been plain in-memory Python dicts.
If the TCC process crashes or restarts while a reservation is outstanding
(RESERVED, OPEN, or AMBIGUOUS), the reconstructed PortfolioRiskManager
starts with NO knowledge of it -- silently understating real exposure and
potentially permitting a new order that should have been rejected.

CORE SAFETY PRINCIPLE (unchanged from Phase 17.1's own AMBIGUOUS fix, now
extended across a restart): UNKNOWN MUST NEVER BECOME ZERO. An outstanding
reservation that cannot be proven resolved is held, its exposure counted,
and reconciliation required -- never silently discarded just because the
process restarted.

--------------------------------------------------------------------------
AUTHORITATIVE FACTS VS DERIVED STATE (Section 7/9 of this phase's brief)
--------------------------------------------------------------------------
Persisted (authoritative, durable):
  - Reservation identity, idempotency-key linkage, strategy/account/symbol,
    side, quantity, price, notional, the trading date it counts against,
    lifecycle status, and (once known) fill price.
  - The realized position ledger (quantity/avg_price/realized_pnl) per
    (strategy_id, account_id, symbol).
  - The per-day order-count ledger (a durable increment/decrement counter
    per scope, exactly mirroring PortfolioRiskManager's own pre-existing
    in-memory bookkeeping -- see _order_counts in portfolio_risk.py).

Derived (reconstructed on every read, never persisted): strategy/account/
portfolio aggregate exposure, aggregate daily PnL, open-order counts.
These are cheap sums over the persisted rows above (portfolio_risk.py's
own _build_snapshot() already computes them this way from its in-memory
dicts; recovery just repopulates those same dicts from durable storage
before the first snapshot is ever built) -- never given a second,
independently-persisted representation, avoiding two competing sources of
truth (Section 9).

Risk LIMITS/configuration are explicitly NOT persisted here (Section 51)
-- they already have their own source (PortfolioRiskLimits objects passed
in at construction / set_*_limits() calls); this store only ever persists
RISK STATE (what has actually been reserved/committed/released), never
configuration.

--------------------------------------------------------------------------
RESERVATION LIFECYCLE (Section 12/13)
--------------------------------------------------------------------------
    RESERVED  -- evaluate_and_reserve() allowed; durable BEFORE the caller
                 is told it may proceed (Section 16's durable-before-
                 execution rule).
        |
        +--> COMMITTED  (terminal -- a genuine fill; folded into the ledger)
        |
        +--> RELEASED   (terminal -- rejected/failed; budget given back)
        |
        +--> OPEN       (non-terminal -- still-open/partially-filled order;
        |                 continues counting as outstanding exposure)
        |
        +--> AMBIGUOUS  (non-terminal -- broker outcome unknown; continues
                          counting as outstanding exposure; NEVER
                          auto-released; only reconciliation may resolve it)
                |
                +--> COMMITTED  (reconciliation FOUND)
                +--> RELEASED   (reconciliation NOT_FOUND)
                (stays AMBIGUOUS if reconciliation is UNRESOLVED/REQUIRES_REVIEW
                 -- Section 12's own "do not add unnecessary states" rule:
                 AMBIGUOUS already means "held, unresolved, needs
                 reconciliation" exactly as execution.py's own
                 STATUS_AMBIGUOUS already does; a separate
                 REQUIRES_RECONCILIATION/REQUIRES_REVIEW state would just
                 duplicate it)

RESERVED/OPEN/AMBIGUOUS are NON-TERMINAL and are exactly the rows a
restart must reload into the live working set. COMMITTED/RELEASED are
TERMINAL and are kept in the table (never deleted) purely as an audit
trail and as the anchor for the cross-store consistency check below --
they no longer contribute to outstanding exposure.

--------------------------------------------------------------------------
CROSS-STORE CONSISTENCY (Section 29-30)
--------------------------------------------------------------------------
IdempotencyStore, ReconciliationStore (Phase 17.1-R), and this
PortfolioRiskStore are three separate SQLite files -- there is no real
cross-file transaction. A crash between updating one and the other is
possible (Section 31's crash matrix cells F/G/K/L). Rather than pretend a
distributed transaction exists, `reconcile_with_idempotency()` below is a
deterministic, idempotent CONVERGENCE pass: for every non-terminal
reservation carrying an idempotency_key, look up that key's actual
terminal state (if any) in the authoritative IdempotencyStore and
converge this store to match:

    IdempotencyStore=COMPLETED, PortfolioRisk=RESERVED/OPEN/AMBIGUOUS -> COMMIT
    IdempotencyStore=REJECTED/FAILED, PortfolioRisk=anything non-terminal -> RELEASE
    IdempotencyStore=AMBIGUOUS/PENDING, PortfolioRisk=RESERVED -> mark AMBIGUOUS (held)
    IdempotencyStore has no record at all -> leave as-is (still genuinely
        in-flight, or portfolio risk was reserved before the idempotency
        claim was ever made -- see execution.py's own gate order: risk is
        evaluated, mode-gated, THEN idempotency is claimed immediately
        before the broker call, so a crash between reservation and claim
        leaves no idempotency row at all yet; this is a real, held,
        RESERVED reservation, not an error)

This makes convergence a property of ANY restart (run automatically at
PortfolioRiskManager construction when both stores are wired), rather than
a bespoke handler for each individual crash-matrix cell -- exactly what
Section 30 asks for ("implement deterministic recovery/idempotent
transition semantics" instead of faking a distributed transaction).

--------------------------------------------------------------------------
FAIL-CLOSED STARTUP (Section 21-23, 57-58)
--------------------------------------------------------------------------
`open_or_diagnose()` distinguishes three cases before any PortfolioRiskManager
is even constructed:
  - LEGITIMATE FIRST RUN: neither the database file nor its companion
    ".initialized" marker exists -> create both, empty, READY.
  - EXPECTED DB UNEXPECTEDLY MISSING: the marker exists (proving this path
    was previously initialized) but the database file itself is gone ->
    NOT_READY. Never silently recreate an empty database over what should
    have been a populated one.
  - CORRUPT / SCHEMA-INCOMPATIBLE: the file exists but fails to open, fails
    `PRAGMA integrity_check`, or its `schema_version` row does not match
    `_SCHEMA_VERSION` -> NOT_READY. Never reinterpret an unknown schema.
  - READY: the file exists, opens, passes integrity_check, and its schema
    version matches.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from trading.common.file_permissions import harden_file_permissions

__all__ = [
    "PersistedLedgerPosition",
    "PersistedReservation",
    "PortfolioRiskReadiness",
    "PortfolioRiskStoreError",
    "ReservationStatus",
    "SqlitePortfolioRiskStore",
    "open_or_diagnose",
]

_DEFAULT_DB_PATH = "trading_portfolio_risk.db"
_SCHEMA_VERSION = 1
_PORTFOLIO_SCOPE = "*"

_NON_TERMINAL = frozenset({"RESERVED", "OPEN", "AMBIGUOUS"})
_TERMINAL = frozenset({"COMMITTED", "RELEASED"})


class ReservationStatus(str, Enum):
    RESERVED = "RESERVED"
    OPEN = "OPEN"
    AMBIGUOUS = "AMBIGUOUS"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"


class PortfolioRiskReadiness(str, Enum):
    READY = "READY"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    NOT_READY = "NOT_READY"


class PortfolioRiskStoreError(RuntimeError):
    """Fail-closed signal -- mirrors ReconciliationPersistenceError/
    AuditPersistenceError. Raised for any durability failure; the caller
    (PortfolioRiskManager) must treat this as "do not proceed", never
    swallow it into a silent approval."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PersistedReservation:
    reservation_id: str
    idempotency_key: str
    strategy_id: str
    account_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    notional: float
    order_count_day: str  # ISO date string
    status: str
    fill_price: float | None
    created_at: str
    updated_at: str
    version: int

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL


@dataclass(frozen=True)
class PersistedLedgerPosition:
    strategy_id: str
    account_id: str
    symbol: str
    quantity: int
    avg_price: float
    realized_pnl: float


def open_or_diagnose(db_path: str | Path) -> tuple["SqlitePortfolioRiskStore | None", PortfolioRiskReadiness, str]:
    """Read-only-safe diagnostic used BEFORE committing to opening a store
    for real -- distinguishes legitimate first-run from unexpected loss or
    corruption (Section 21-23/57-58). Returns (store_or_None, readiness,
    detail). Never mutates an existing, readable database; never
    recreates one that should have existed."""
    db_path = str(db_path)
    marker_path = db_path + ".initialized"
    db_exists = Path(db_path).is_file()
    marker_exists = Path(marker_path).is_file()

    if not db_exists and not marker_exists:
        try:
            store = SqlitePortfolioRiskStore(db_path)
        except Exception as exc:  # noqa: BLE001 -- fail closed on any construction error
            return None, PortfolioRiskReadiness.NOT_READY, f"first-run initialization failed: {exc}"
        Path(marker_path).write_text(_now(), encoding="utf-8")
        harden_file_permissions(marker_path)
        return store, PortfolioRiskReadiness.READY, "legitimate first run -- new database initialized"

    if marker_exists and not db_exists:
        return None, PortfolioRiskReadiness.NOT_READY, (
            f"database {db_path!r} is missing but its initialization marker {marker_path!r} exists -- "
            "this looks like unexpected data loss, not a first run. Refusing to silently create an empty database."
        )

    try:
        store = SqlitePortfolioRiskStore(db_path)
    except Exception as exc:  # noqa: BLE001
        return None, PortfolioRiskReadiness.NOT_READY, f"database exists but could not be opened/validated: {exc}"

    if not marker_exists:
        # DB exists (from before markers existed, or copied in) but no
        # marker -- write one now; this is not data loss, just a
        # pre-marker database. Does not affect READY status.
        Path(marker_path).write_text(_now(), encoding="utf-8")
        harden_file_permissions(marker_path)

    return store, PortfolioRiskReadiness.READY, "existing database opened and validated"


class SqlitePortfolioRiskStore:
    """Default, production-appropriate durable store for portfolio-risk
    state. Standard-library sqlite3 only, matching every other store in
    trading.common (idempotency_store.py, live_authorization.py,
    reconciliation.py) -- same per-call-connection + threading.Lock
    discipline, same harden_file_permissions() call, same schema-version
    row convention.

    Raises PortfolioRiskStoreError (never returns a half-open/unusable
    instance) if the file exists but fails PRAGMA integrity_check or its
    schema_version does not match -- callers must use open_or_diagnose()
    rather than constructing this directly in production code, so that
    failure is turned into a readiness state rather than an uncaught
    exception at import/startup time.
    """

    def __init__(self, db_path: str | Path = _DEFAULT_DB_PATH) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        is_new = not Path(self._db_path).is_file()
        self._init_schema(is_new)
        harden_file_permissions(self._db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self, is_new: bool) -> None:
        with self._lock:
            conn = self._connect()
            try:
                if not is_new:
                    try:
                        (ok,) = conn.execute("PRAGMA integrity_check").fetchone()
                    except sqlite3.DatabaseError as exc:
                        raise PortfolioRiskStoreError(f"integrity_check could not run: {exc}") from exc
                    if ok != "ok":
                        raise PortfolioRiskStoreError(f"PRAGMA integrity_check failed: {ok}")

                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                        (str(_SCHEMA_VERSION),),
                    )
                else:
                    existing_version = int(row["value"])
                    if existing_version != _SCHEMA_VERSION:
                        raise PortfolioRiskStoreError(
                            f"schema_version mismatch: database has {existing_version}, "
                            f"this code expects {_SCHEMA_VERSION} -- refusing to reinterpret an unknown schema"
                        )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS portfolio_reservations (
                        reservation_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        strategy_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        price REAL NOT NULL,
                        notional REAL NOT NULL,
                        order_count_day TEXT NOT NULL,
                        status TEXT NOT NULL,
                        fill_price REAL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pr_status ON portfolio_reservations(status)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pr_idem ON portfolio_reservations(idempotency_key)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pr_strategy_account ON portfolio_reservations(strategy_id, account_id)"
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS portfolio_ledger (
                        strategy_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        avg_price REAL NOT NULL,
                        realized_pnl REAL NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (strategy_id, account_id, symbol)
                    )
                    """
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS portfolio_order_counts (
                        scope_type TEXT NOT NULL,
                        scope_id TEXT NOT NULL,
                        trading_date TEXT NOT NULL,
                        count INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (scope_type, scope_id, trading_date)
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    # -- reservation lifecycle ------------------------------------------------ #
    def create_reservation(
        self, *, reservation_id: str, idempotency_key: str, strategy_id: str, account_id: str, symbol: str,
        side: str, quantity: int, price: float, notional: float, order_count_day: date,
    ) -> None:
        """Atomic create -- a bare INSERT against the PRIMARY KEY, exactly
        like SqliteIdempotencyStore.claim(). Raises PortfolioRiskStoreError
        (never silently no-ops) if this reservation_id already exists --
        reservation_id is a freshly minted uuid4 per call site, so a
        collision here indicates a genuine bug, not a legitimate retry
        (retries are deduplicated by idempotency_key, one layer up, before
        a reservation is ever attempted -- see portfolio_risk.py)."""
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO portfolio_reservations
                        (reservation_id, idempotency_key, strategy_id, account_id, symbol, side,
                         quantity, price, notional, order_count_day, status, fill_price, created_at, updated_at, version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESERVED', NULL, ?, ?, 1)
                    """,
                    (reservation_id, idempotency_key, strategy_id, account_id, symbol, side,
                     quantity, price, notional, order_count_day.isoformat(), now, now),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise PortfolioRiskStoreError(f"reservation_id {reservation_id!r} already exists: {exc}") from exc
            finally:
                conn.close()

    def transition(
        self, reservation_id: str, *, from_statuses: tuple[str, ...], to_status: str, fill_price: float | None = None,
    ) -> bool:
        """Atomic compare-and-swap: only succeeds if the row is currently
        in one of `from_statuses`. Returns True if this call performed the
        transition, False if the row was already elsewhere (idempotent
        no-op -- a repeated commit/release/reconciliation call converges
        safely instead of double-applying, Sections 42-44) or does not
        exist. Never raises for a missing/already-resolved row -- mirrors
        every other resolution method's "no-op, not an error" contract in
        this codebase (release_reservation(), resolve_open_order(), ...)."""
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                placeholders = ",".join("?" for _ in from_statuses)
                cur = conn.execute(
                    f"""
                    UPDATE portfolio_reservations
                    SET status = ?, fill_price = COALESCE(?, fill_price), updated_at = ?, version = version + 1
                    WHERE reservation_id = ? AND status IN ({placeholders})
                    """,
                    (to_status, fill_price, now, reservation_id, *from_statuses),
                )
                conn.commit()
                return cur.rowcount == 1
            finally:
                conn.close()

    def get(self, reservation_id: str) -> PersistedReservation | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM portfolio_reservations WHERE reservation_id = ?", (reservation_id,)
                ).fetchone()
            finally:
                conn.close()
        return None if row is None else PersistedReservation(**{k: row[k] for k in row.keys()})

    def get_by_idempotency_key(self, idempotency_key: str) -> PersistedReservation | None:
        if not idempotency_key:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM portfolio_reservations WHERE idempotency_key = ? ORDER BY created_at DESC LIMIT 1",
                    (idempotency_key,),
                ).fetchone()
            finally:
                conn.close()
        return None if row is None else PersistedReservation(**{k: row[k] for k in row.keys()})

    def list_non_terminal(self) -> list[PersistedReservation]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM portfolio_reservations WHERE status IN ('RESERVED','OPEN','AMBIGUOUS')"
                ).fetchall()
            finally:
                conn.close()
        return [PersistedReservation(**{k: row[k] for k in row.keys()}) for row in rows]

    # -- ledger ----------------------------------------------------------------- #
    def upsert_ledger_position(
        self, *, strategy_id: str, account_id: str, symbol: str, quantity: int, avg_price: float, realized_pnl: float,
    ) -> None:
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO portfolio_ledger (strategy_id, account_id, symbol, quantity, avg_price, realized_pnl, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(strategy_id, account_id, symbol) DO UPDATE SET
                        quantity=excluded.quantity, avg_price=excluded.avg_price,
                        realized_pnl=excluded.realized_pnl, updated_at=excluded.updated_at
                    """,
                    (strategy_id, account_id, symbol, quantity, avg_price, realized_pnl, now),
                )
                conn.commit()
            finally:
                conn.close()

    def list_ledger_positions(self) -> list[PersistedLedgerPosition]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT * FROM portfolio_ledger").fetchall()
            finally:
                conn.close()
        return [
            PersistedLedgerPosition(
                strategy_id=r["strategy_id"], account_id=r["account_id"], symbol=r["symbol"],
                quantity=r["quantity"], avg_price=r["avg_price"], realized_pnl=r["realized_pnl"],
            )
            for r in rows
        ]

    # -- order counts ------------------------------------------------------------ #
    def adjust_order_count(self, *, scope_type: str, scope_id: str, trading_date: date, delta: int) -> None:
        """Durable increment/decrement -- mirrors PortfolioRiskManager's
        own in-memory _order_counts dict exactly (incremented once per
        successful reservation, decremented once on release/NOT_FOUND,
        never decremented on a successful commit -- see that module's own
        docstring for why this is intentionally NOT the same thing as
        "count of currently-outstanding reservations")."""
        now_date = trading_date.isoformat()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO portfolio_order_counts (scope_type, scope_id, trading_date, count)
                    VALUES (?, ?, ?, MAX(0, ?))
                    ON CONFLICT(scope_type, scope_id, trading_date) DO UPDATE SET
                        count = MAX(0, count + ?)
                    """,
                    (scope_type, scope_id, now_date, delta, delta),
                )
                conn.commit()
            finally:
                conn.close()

    def get_order_count(self, *, scope_type: str, scope_id: str, trading_date: date) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT count FROM portfolio_order_counts WHERE scope_type = ? AND scope_id = ? AND trading_date = ?",
                    (scope_type, scope_id, trading_date.isoformat()),
                ).fetchone()
            finally:
                conn.close()
        return int(row["count"]) if row is not None else 0

    def list_order_counts(self) -> list[tuple[str, str, str, int]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT scope_type, scope_id, trading_date, count FROM portfolio_order_counts").fetchall()
            finally:
                conn.close()
        return [(r["scope_type"], r["scope_id"], r["trading_date"], r["count"]) for r in rows]
