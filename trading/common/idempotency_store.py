"""
Phase 14.6 -- Blocker D: persistent, AUTHORITATIVE idempotency / order-
execution state, surviving process restart, deployment, crash, or
container/EC2 restart.

Before Phase 14.6, RiskManager and LiveCanaryGuard each tracked
"idempotency keys already seen" in a plain in-memory set -- empty again
after every restart. That is a real duplicate-order-placement hazard: a
caller retrying an intent with the same idempotency_key after a restart
would not be recognized as a duplicate, and a second real broker order
could be submitted.

This module is the fix. StrategyExecutionEngine.execute() consults an
IdempotencyStore FIRST (before RiskManager, before LiveCanaryGuard, before
any broker call) for any intent carrying a non-empty idempotency_key:

    SAME IDEMPOTENCY KEY, record already exists
        -> return the CACHED ExecutionResult
        -> the broker is NEVER called again -- not even after a restart,
           because the store itself (not any in-memory set) is what's
           consulted.

    NEW IDEMPOTENCY KEY (or none supplied)
        -> proceed through the normal pipeline as before.

A record is only ever written AFTER a broker call has definitively
returned an answer (success or a broker-reported rejection) -- see
execute()'s own comments for exactly where. A genuinely ambiguous outcome
(the broker never answered at all -- StrategyExecutionEngine's own retry
loop exhausts and returns None) is deliberately NOT persisted as "done":
whether an order actually reached the broker in that case is unknown, and
inventing a cached answer would be worse than leaving it retryable. This
is an honest, documented boundary (the real fix for genuine ambiguity is
position reconciliation against the broker's own records, not
idempotency alone -- see docs/phase-14-6-safety-hardening-report.md).

RiskManager's and LiveCanaryGuard's own in-memory duplicate-key sets are
UNCHANGED and remain in place as a fast, additional, pre-trade heuristic
-- this store is authoritative and checked independently, not a
replacement for either.

Storage: this module defines a narrow `IdempotencyStore` Protocol so
trading.common stays free of any concrete storage dependency (the same
layering discipline every trading.common module has kept since Phase 1 --
none of them import trading.database or trading.api). The default,
production-appropriate implementation, `SqliteIdempotencyStore`, uses only
the Python standard library's sqlite3 module -- no new project dependency
-- writing to a single file that survives everything this phase's brief
lists (restart, deployment, crash, container/EC2 restart), which is
exactly what "the project's existing persistence mechanism" already does
by default for its own control-center database (trading.database.
connection's own default DATABASE_URL is a local SQLite file) -- this
reuses that same class of mechanism as trading.common's own file, rather
than importing the API layer's SQLAlchemy ORM into the core execution
framework and inverting that layering.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from trading.common.file_permissions import harden_file_permissions

_DEFAULT_DB_PATH = "trading_idempotency.db"

STATUS_COMPLETED = "COMPLETED"
STATUS_REJECTED = "REJECTED"
STATUS_FAILED = "FAILED"
#: Phase 15D-DR (Area E/J): the broker call that would have submitted this
#: intent raised an exception whose outcome cannot be determined -- the
#: order may or may not actually exist at the broker. Deliberately distinct
#: from STATUS_FAILED (which means "definitively did not succeed" -- e.g. a
#: broker-response-validation failure on a response that WAS received).
#: A record in this state must never be silently replayed as success,
#: silently treated as safe-to-resubmit, or silently ignored -- see
#: AmbiguousIdempotencyStateError below.
STATUS_AMBIGUOUS = "AMBIGUOUS"
#: Phase 15D-DR (Area C.8 / M.7): a placeholder status written by claim()
#: the instant a request begins processing a NEW idempotency_key, closing
#: the race window between "check whether this key exists" and "persist
#: the definitive outcome" that two concurrent requests for the SAME key
#: could otherwise both pass through. A record left in this state (e.g.
#: the process crashed mid-execution) is exactly as unresolved as
#: STATUS_AMBIGUOUS and must be handled identically -- never silently
#: replayed, never silently treated as safe-to-resubmit.
STATUS_PENDING = "PENDING"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class IdempotencyRecord:
    idempotency_key: str
    strategy_id: str
    account_id: str
    intent_hash: str
    status: str  # STATUS_COMPLETED | STATUS_REJECTED | STATUS_FAILED
    broker_order_id: str
    result_json: str  # a serialized ExecutionResult, for exact replay
    created_at: str
    updated_at: str


class IdempotencyKeyReuseError(RuntimeError):
    """Raised when the SAME idempotency_key is reused for a genuinely
    DIFFERENT intent (different symbol/side/quantity/account/...) --
    treated as an error, never as "close enough, replay the old result"
    or "ignore the key and proceed", either of which could hide a caller
    bug behind a wrong, silently-replayed outcome."""


class AmbiguousIdempotencyStateError(RuntimeError):
    """Phase 15D-DR: raised when a replay lookup finds an existing record
    for this exact idempotency_key (same intent) whose status is
    STATUS_AMBIGUOUS -- the prior attempt's broker outcome was never
    determined. Never silently replayed, and never silently allowed to
    proceed to a fresh broker submission (which could create a duplicate
    real order for an intent that may have already succeeded). The caller
    must reconcile the actual broker state (order book / positions) before
    any further action on this key is possible."""


class IdempotencyStore(Protocol):
    """The narrow interface StrategyExecutionEngine depends on. Any
    persistent (or, for tests only, in-memory) implementation satisfying
    this is acceptable -- trading.common never depends on a concrete
    storage technology."""

    def get(self, idempotency_key: str) -> IdempotencyRecord | None: ...

    def put(self, record: IdempotencyRecord) -> None: ...

    def list_by_status(self, status: str) -> list[IdempotencyRecord]:
        """Phase 15D-RECON: enumerate every record currently at `status` --
        added specifically so a reconciliation subsystem can discover
        outstanding STATUS_AMBIGUOUS/STATUS_PENDING records without already
        knowing their idempotency_key (get() requires the key; this doesn't).
        Purely additive and read-only -- does not change get()/put()/claim()
        in any way."""
        ...

    def claim(self, idempotency_key: str, *, strategy_id: str, account_id: str, intent_hash: str) -> bool:
        """Phase 15D-DR: atomically create a STATUS_PENDING placeholder
        for `idempotency_key` IF AND ONLY IF no record exists yet.
        Returns True if this call was the one that created it (the caller
        may proceed), False if a record already existed (another request
        already claimed -- or completed -- this key; the caller must NOT
        proceed to a fresh broker submission). Must be atomic with respect
        to concurrent callers -- a plain get()-then-put() is NOT sufficient,
        which is exactly the race this method exists to close."""
        ...

    def resolve_ambiguous(
        self, idempotency_key: str, *, new_status: str, broker_order_id: str = "", result_json: str = "",
    ) -> bool:
        """Phase 17.1-R Remediation E: an atomic compare-and-swap that
        transitions a record FROM STATUS_AMBIGUOUS/STATUS_PENDING ONLY to a
        new terminal status (`new_status` must not itself be AMBIGUOUS/
        PENDING) -- the reconciliation subsystem's sole write path back into
        this store. Returns True if this call performed the transition,
        False if the record did not exist or was not at AMBIGUOUS/PENDING
        (already resolved by a concurrent caller, or never ambiguous in the
        first place) -- never raises, never overwrites an already-terminal
        record, never deletes history. A caller MUST treat False as "someone
        else already resolved this (or it wasn't ambiguous) -- do nothing
        further", exactly like claim()'s own False case."""
        ...


def compute_intent_hash(intent) -> str:  # noqa: ANN001 -- trading.common.order_intent.OrderIntent, avoiding an import cycle concern is moot but kept loose intentionally
    """A stable identity hash of an OrderIntent's TRADEABLE content --
    deliberately excludes client_order_id/correlation_id/created_at
    (always unique per construction, would defeat the whole point) but
    includes idempotency_key itself, so two different keys never collide
    here and a genuine key-reuse-for-a-different-intent is always
    detectable by comparing this hash against what's stored."""
    payload = "|".join([
        intent.strategy_id, intent.account_id, intent.symbol, intent.exchange,
        intent.side.value, str(intent.quantity), intent.order_type.value,
        "" if intent.limit_price is None else str(intent.limit_price),
        intent.idempotency_key,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SqliteIdempotencyStore:
    """Default, production-appropriate IdempotencyStore -- a single
    SQLite file, standard library only. AUTHORITATIVE: this is the only
    thing StrategyExecutionEngine.execute() trusts to answer "have we
    already done this" -- never an in-memory cache.

    A short-lived connection is opened per call rather than held open
    across the instance's lifetime -- sqlite3 connections are not safe to
    share across threads by default, and StrategyExecutionEngine already
    runs background threads (its own pending-order management) alongside
    the calling thread; a fresh connection per call sidesteps that
    entirely at a small, acceptable overhead cost for an operation this
    infrequent (once per order, not once per tick)."""

    def __init__(self, db_path: str | Path = _DEFAULT_DB_PATH) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_schema()
        harden_file_permissions(self._db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS idempotency_records (
                        idempotency_key TEXT PRIMARY KEY,
                        strategy_id TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        intent_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        broker_order_id TEXT NOT NULL DEFAULT '',
                        result_json TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def get(self, idempotency_key: str) -> IdempotencyRecord | None:
        if not idempotency_key:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM idempotency_records WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return IdempotencyRecord(**{k: row[k] for k in row.keys()})

    def put(self, record: IdempotencyRecord) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO idempotency_records
                        (idempotency_key, strategy_id, account_id, intent_hash, status,
                         broker_order_id, result_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(idempotency_key) DO UPDATE SET
                        status=excluded.status, broker_order_id=excluded.broker_order_id,
                        result_json=excluded.result_json, updated_at=excluded.updated_at
                    """,
                    (
                        record.idempotency_key, record.strategy_id, record.account_id, record.intent_hash,
                        record.status, record.broker_order_id, record.result_json,
                        record.created_at, record.updated_at,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def claim(self, idempotency_key: str, *, strategy_id: str, account_id: str, intent_hash: str) -> bool:
        """A bare INSERT (never ON CONFLICT) against the PRIMARY KEY column
        -- SQLite raises IntegrityError if a row already exists, which is
        exactly the atomicity this needs: whichever caller's INSERT commits
        first wins the claim, and every other concurrent caller's INSERT
        fails, both under the SAME `self._lock` that serializes every other
        method here, and (for true multi-process deployments) under
        SQLite's own file-level locking."""
        if not idempotency_key:
            return True  # no key at all -- nothing to claim/protect (matches _persist_idempotency's own no-op)
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO idempotency_records
                        (idempotency_key, strategy_id, account_id, intent_hash, status,
                         broker_order_id, result_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, '', '', ?, ?)
                    """,
                    (idempotency_key, strategy_id, account_id, intent_hash, STATUS_PENDING, now, now),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False  # someone else already claimed (or completed) this key
            finally:
                conn.close()

    def list_by_status(self, status: str) -> list[IdempotencyRecord]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT * FROM idempotency_records WHERE status = ?", (status,)
                ).fetchall()
            finally:
                conn.close()
        return [IdempotencyRecord(**{k: row[k] for k in row.keys()}) for row in rows]

    def resolve_ambiguous(
        self, idempotency_key: str, *, new_status: str, broker_order_id: str = "", result_json: str = "",
    ) -> bool:
        if new_status in (STATUS_AMBIGUOUS, STATUS_PENDING):
            raise ValueError(f"resolve_ambiguous() cannot resolve TO {new_status!r} -- that is not a resolution")
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE idempotency_records SET
                        status = ?, broker_order_id = COALESCE(NULLIF(?, ''), broker_order_id),
                        result_json = COALESCE(NULLIF(?, ''), result_json), updated_at = ?
                    WHERE idempotency_key = ? AND status IN (?, ?)
                    """,
                    (new_status, broker_order_id, result_json, now, idempotency_key, STATUS_AMBIGUOUS, STATUS_PENDING),
                )
                conn.commit()
                return cur.rowcount == 1
            finally:
                conn.close()


class InMemoryIdempotencyStore:
    """Explicitly NOT for production use -- does not survive a restart,
    which defeats Blocker D's entire purpose. Exists only for tests that
    want isolation without touching the filesystem; the name is
    deliberately loud so it is never mistaken for the authoritative
    default."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, IdempotencyRecord] = {}

    def get(self, idempotency_key: str) -> IdempotencyRecord | None:
        with self._lock:
            return self._records.get(idempotency_key)

    def put(self, record: IdempotencyRecord) -> None:
        with self._lock:
            self._records[record.idempotency_key] = record

    def claim(self, idempotency_key: str, *, strategy_id: str, account_id: str, intent_hash: str) -> bool:
        if not idempotency_key:
            return True
        with self._lock:
            if idempotency_key in self._records:
                return False
            now = _now()
            self._records[idempotency_key] = IdempotencyRecord(
                idempotency_key=idempotency_key, strategy_id=strategy_id, account_id=account_id,
                intent_hash=intent_hash, status=STATUS_PENDING, broker_order_id="", result_json="",
                created_at=now, updated_at=now,
            )
            return True

    def list_by_status(self, status: str) -> list[IdempotencyRecord]:
        with self._lock:
            return [r for r in self._records.values() if r.status == status]

    def resolve_ambiguous(
        self, idempotency_key: str, *, new_status: str, broker_order_id: str = "", result_json: str = "",
    ) -> bool:
        if new_status in (STATUS_AMBIGUOUS, STATUS_PENDING):
            raise ValueError(f"resolve_ambiguous() cannot resolve TO {new_status!r} -- that is not a resolution")
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is None or existing.status not in (STATUS_AMBIGUOUS, STATUS_PENDING):
                return False
            self._records[idempotency_key] = IdempotencyRecord(
                idempotency_key=existing.idempotency_key, strategy_id=existing.strategy_id,
                account_id=existing.account_id, intent_hash=existing.intent_hash, status=new_status,
                broker_order_id=broker_order_id or existing.broker_order_id,
                result_json=result_json or existing.result_json,
                created_at=existing.created_at, updated_at=_now(),
            )
            return True
