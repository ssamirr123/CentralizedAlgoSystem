"""
Phase 15D-RECON -- persistent, read-only broker reconciliation.

Answers exactly one question, durably: "did the broker actually receive/
accept/fill this execution?" -- for any OrderIntent whose outcome Phase
14.6/15D-DR left unresolved (`STATUS_AMBIGUOUS`: the mutating broker call
raised mid-flight; `STATUS_PENDING`: a claim was made but the process died
before a definitive outcome was persisted). See
trading.common.idempotency_store's own module docstring for why those two
states exist and are never silently replayed or resubmitted.

--------------------------------------------------------------------------
CORE SAFETY PRINCIPLE -- RECONCILIATION IS READ-ONLY
--------------------------------------------------------------------------
This module never places, retries, cancels, modifies, or replaces an
order, and never averages/hedges/closes a position. That is not just a
convention followed by this file's own code -- it is enforced
structurally by `ReadOnlyBrokerView`: the only object `ReconciliationService`
ever touches a `BrokerClient` through. `ReadOnlyBrokerView` does not
DEFINE `place_order`/`cancel_order`/`modify_order` at all -- there is no
method to accidentally call, not merely an unused one. See
`test_read_only_broker_view_has_no_mutating_methods` in this phase's test
file for the proof.

A reconciliation RESULT (RECONCILED_FILLED/REJECTED/NOT_FOUND/UNKNOWN/
RECONCILIATION_FAILED) is a plain data value written to durable storage
and to the audit trail.

Phase 17.1-R Remediation E (extending this same module, not a second
subsystem -- see `_feed_back_to_idempotency_and_risk` below): a TERMINAL
result (FILLED/ACCEPTED/REJECTED/NOT_FOUND) is additionally fed back, via
one atomic compare-and-swap (`IdempotencyStore.resolve_ambiguous()`), into
the ORIGINAL idempotency record this reconciliation was resolving --
transitioning it from AMBIGUOUS/PENDING to a genuine terminal COMPLETED
(FOUND) or REJECTED (NOT_FOUND) outcome, so a future retry of that exact
idempotency_key is replayed from the resolved record and never calls the
broker again. If a `PortfolioRiskManager` was wired in, the SAME resolution
also commits (FOUND) or releases (NOT_FOUND) whatever reservation
Section 27's AMBIGUOUS-outcome fix left held open. A non-terminal result
(RECONCILIATION_UNKNOWN/FAILED, "evidence insufficient") changes NEITHER --
both stay held/unresolved, requiring operator review, exactly as before.

This feedback is still never a broker-mutating call, never a retry, never
a new order, and never touches `StrategyExecutionEngine`'s per-order
`RiskManager.validate()` gate (which only ever runs for a NEW intent, not a
resolved historical one) -- it only ever changes durable BOOKKEEPING state
about an order that has ALREADY happened (or definitively never happened).
Turning "an order came back ambiguous" into NEW corrective trading action
(a future auto-reconciliation-with-hedge feature, an automatic replacement
order, ...) remains explicitly out of scope and explicitly NOT built here;
Section 23's policy is deliberately fail-closed: NOT_FOUND releases the
reservation and resolves the record, but authorizes no new attempt.

--------------------------------------------------------------------------
Where "expected" comes from
--------------------------------------------------------------------------
Neither `IdempotencyRecord` (Phase 14.6/15D-DR) nor this phase's brief
asked for a redesign of that store's schema (Rule 1: don't redesign
working logic unnecessarily) -- it only ever kept a HASH of the intent's
tradeable fields, deliberately, for identity checking, never the fields
themselves. The one durable place the actual symbol/side/quantity/
order_type/exchange/product_type/expiry/strike/option_type were already
being recorded is Phase 15D-AUDIT's `EVENT_ORDER_INTENT_CREATED` audit
event, keyed by `idempotency_key` (already indexed --
`PersistentAuditTrail.by_idempotency_key()`). So `_reconstruct_expected()`
below reads that event back out of the audit trail rather than adding a
second, parallel place to store the same data -- one durable source of
truth, not two that could drift apart. (`execution.py`'s own
`EVENT_ORDER_INTENT_CREATED` call was extended, additively, this phase to
also carry `exchange`/`product_type`/`expiry`/`strike`/`option_type` --
it previously carried only `symbol`/`side`/`quantity`/`order_type`.)

--------------------------------------------------------------------------
Lifecycle
--------------------------------------------------------------------------
    STATUS_AMBIGUOUS / STATUS_PENDING (idempotency_store)
            |
            v   ReconciliationService.scan_and_register() -- read-only,
            |   idempotent (safe to call repeatedly / on every restart)
            v
    RECONCILIATION_REQUIRED (reconciliation_records, durable)
            |
            v   ReconciliationService.reconcile_one() --
            |     1. try_acquire() -- atomic, persistence-backed (Area L)
            |     2. RECONCILIATION_STARTED audit event -- fail CLOSED if
            |        this can't be written (Area C)
            |     3. read-only broker query via ReadOnlyBrokerView (Area D)
            |     4. expected-vs-actual comparison (Area E/F)
            v
    RECONCILED_FILLED | RECONCILED_ACCEPTED | RECONCILED_REJECTED |
    RECONCILED_NOT_FOUND | RECONCILIATION_UNKNOWN | RECONCILIATION_FAILED
            |
            v
    RECONCILIATION_COMPLETED audit event (best-effort past this point --
    the definitive answer is already durably in reconciliation_records)
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any

from trading.common.audit_store import (
    EVENT_RECONCILIATION_COMPLETED,
    EVENT_RECONCILIATION_FAILED,
    EVENT_RECONCILIATION_STARTED,
    redact_string,
)
from trading.common.observability import EVENT_ORDER_INTENT_CREATED
from trading.common.broker import BrokerClient, BrokerConnectionError
from trading.common.deployment_info import get_deployment_info
from trading.common.file_permissions import harden_file_permissions
from trading.common.execution import ExecutionResult
from trading.common.idempotency_store import (
    IdempotencyStore,
    STATUS_AMBIGUOUS,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_REJECTED,
)

_DEFAULT_DB_PATH = "trading_reconciliation.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReconciliationStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    RECONCILIATION_IN_PROGRESS = "RECONCILIATION_IN_PROGRESS"
    RECONCILED_ACCEPTED = "RECONCILED_ACCEPTED"
    RECONCILED_FILLED = "RECONCILED_FILLED"
    RECONCILED_REJECTED = "RECONCILED_REJECTED"
    RECONCILED_NOT_FOUND = "RECONCILED_NOT_FOUND"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    RECONCILIATION_UNKNOWN = "RECONCILIATION_UNKNOWN"


_OUTSTANDING = (ReconciliationStatus.RECONCILIATION_REQUIRED.value, ReconciliationStatus.RECONCILIATION_IN_PROGRESS.value)
_TERMINAL = {
    ReconciliationStatus.RECONCILED_ACCEPTED.value, ReconciliationStatus.RECONCILED_FILLED.value,
    ReconciliationStatus.RECONCILED_REJECTED.value, ReconciliationStatus.RECONCILED_NOT_FOUND.value,
    ReconciliationStatus.RECONCILIATION_FAILED.value, ReconciliationStatus.RECONCILIATION_UNKNOWN.value,
}


class ReconciliationPersistenceError(RuntimeError):
    """Fail-closed signal -- mirrors trading.common.audit_store's
    AuditPersistenceError. Raised whenever a reconciliation state
    transition could not be durably committed; never swallowed into a
    silent "pretend it worked"."""


class ReconciliationOwnershipError(ReconciliationPersistenceError):
    """Raised by complete()/mark_failed() when the caller is no longer (or
    never was) the owner of an IN_PROGRESS record -- e.g. a stale worker
    finishing after reclaim_stale() already handed the record to someone
    else. Refusing to write here is what prevents two workers' results
    from silently clobbering each other."""


@dataclass(frozen=True)
class ReconciliationRecord:
    reconciliation_id: str
    idempotency_key: str
    correlation_id: str
    strategy_id: str
    account_id: str
    broker_id: str
    symbol: str
    exchange: str
    expiry: str
    strike: float | None
    option_type: str
    expected_side: str
    expected_quantity: int
    expected_order_type: str
    expected_product_type: str
    broker_order_id: str
    status: str
    broker_status: str
    filled_quantity: int
    average_price: float | None
    rejection_reason: str
    error_message: str
    owner: str
    started_at: str
    claimed_at: str
    completed_at: str
    app_version: str
    git_sha: str
    deployment_id: str
    environment: str
    created_at: str
    updated_at: str
    version: int

    @property
    def is_outstanding(self) -> bool:
        return self.status in _OUTSTANDING

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL


def _derive_reconciliation_id(idempotency_key: str) -> str:
    """Deterministic: the SAME idempotency_key always maps to the SAME
    reconciliation_id, which is what makes create_required() naturally
    idempotent (INSERT OR IGNORE against this as the primary key) -- a
    scanner run twice (e.g. once before and once after a restart) never
    creates a duplicate record for the same underlying execution attempt."""
    return "RECON-" + sha256(idempotency_key.encode("utf-8")).hexdigest()[:20]


class SqliteReconciliationStore:
    """Durable, transactional reconciliation state. Standard-library
    sqlite3 only, same per-call-connection + lock discipline as every
    other store in trading.common (idempotency_store.py, audit_store.py).

    Deliberately mutable (UPDATE is used, unlike audit_store.py's
    append-only design) -- a reconciliation record legitimately transitions
    through states over its lifetime; ownership/version guards on every
    UPDATE (see try_acquire()/complete()/mark_failed()/reclaim_stale())
    are what make concurrent access to the SAME record safe, not
    immutability. The full auditable HISTORY of what happened is the
    separate, append-only audit trail (RECONCILIATION_STARTED/COMPLETED
    events) -- this table is current authoritative state, not a log.

    No credential/secret field exists anywhere in this schema -- every
    column is a specific, named, non-secret value (symbol, quantity,
    status, ...); there is no generic metadata/detail blob a caller could
    accidentally stash a secret into (Area A: "do not store credentials or
    secrets", satisfied structurally, not by runtime scrubbing). Free-text
    fields this module itself constructs (`error_message`/
    `rejection_reason`, sourced from a broker exception's own message) are
    still passed through `trading.common.audit_store.redact_string()` as
    defense-in-depth against an SDK echoing something secret-shaped back.
    """

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
                    CREATE TABLE IF NOT EXISTS reconciliation_records (
                        reconciliation_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        correlation_id TEXT NOT NULL DEFAULT '',
                        strategy_id TEXT NOT NULL DEFAULT '',
                        account_id TEXT NOT NULL DEFAULT '',
                        broker_id TEXT NOT NULL DEFAULT '',
                        symbol TEXT NOT NULL DEFAULT '',
                        exchange TEXT NOT NULL DEFAULT '',
                        expiry TEXT NOT NULL DEFAULT '',
                        strike REAL,
                        option_type TEXT NOT NULL DEFAULT '',
                        expected_side TEXT NOT NULL DEFAULT '',
                        expected_quantity INTEGER NOT NULL DEFAULT 0,
                        expected_order_type TEXT NOT NULL DEFAULT '',
                        expected_product_type TEXT NOT NULL DEFAULT '',
                        broker_order_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        broker_status TEXT NOT NULL DEFAULT '',
                        filled_quantity INTEGER NOT NULL DEFAULT 0,
                        average_price REAL,
                        rejection_reason TEXT NOT NULL DEFAULT '',
                        error_message TEXT NOT NULL DEFAULT '',
                        owner TEXT NOT NULL DEFAULT '',
                        started_at TEXT NOT NULL DEFAULT '',
                        claimed_at TEXT NOT NULL DEFAULT '',
                        completed_at TEXT NOT NULL DEFAULT '',
                        app_version TEXT NOT NULL DEFAULT '',
                        git_sha TEXT NOT NULL DEFAULT '',
                        deployment_id TEXT NOT NULL DEFAULT '',
                        environment TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1
                    )
                    """
                )
                for col in ("account_id", "status", "correlation_id", "broker_order_id"):
                    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_recon_{col} ON reconciliation_records({col})")
                conn.commit()
            finally:
                conn.close()

    def _row_to_record(self, row: sqlite3.Row) -> ReconciliationRecord:
        return ReconciliationRecord(**{k: row[k] for k in row.keys()})

    # -- creation (idempotent) -------------------------------------------- #
    def create_required(
        self, *, idempotency_key: str, correlation_id: str = "", strategy_id: str = "", account_id: str = "",
        broker_id: str = "", symbol: str = "", exchange: str = "", expiry: str = "", strike: float | None = None,
        option_type: str = "", expected_side: str = "", expected_quantity: int = 0,
        expected_order_type: str = "", expected_product_type: str = "", broker_order_id: str = "",
    ) -> ReconciliationRecord:
        if not idempotency_key:
            raise ReconciliationPersistenceError("idempotency_key is required to create a reconciliation record")
        reconciliation_id = _derive_reconciliation_id(idempotency_key)
        now = _now()
        info = get_deployment_info()
        with self._lock:
            try:
                conn = self._connect()
                try:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO reconciliation_records
                            (reconciliation_id, idempotency_key, correlation_id, strategy_id, account_id, broker_id,
                             symbol, exchange, expiry, strike, option_type, expected_side, expected_quantity,
                             expected_order_type, expected_product_type, broker_order_id, status, broker_status,
                             filled_quantity, average_price, rejection_reason, error_message, owner, started_at,
                             claimed_at, completed_at, app_version, git_sha, deployment_id, environment,
                             created_at, updated_at, version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, NULL, '', '', '', '', '', '',
                                ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            reconciliation_id, idempotency_key, correlation_id, strategy_id, account_id, broker_id,
                            symbol, exchange, expiry, strike, option_type, expected_side, expected_quantity,
                            expected_order_type, expected_product_type, broker_order_id,
                            ReconciliationStatus.RECONCILIATION_REQUIRED.value,
                            info.app_version, info.git_sha, info.deployment_id, info.environment, now, now,
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except sqlite3.OperationalError as exc:
                raise ReconciliationPersistenceError(f"reconciliation database unavailable/locked: {exc}") from exc
        record = self.get(reconciliation_id)
        assert record is not None  # INSERT OR IGNORE guarantees a row exists at this id either way
        return record

    # -- concurrency-safe claim (Area L) ------------------------------------ #
    def try_acquire(self, reconciliation_id: str, owner: str) -> bool:
        """Atomic, persistence-backed compare-and-swap: only succeeds if
        the record is currently RECONCILIATION_REQUIRED. Two callers racing
        for the SAME reconciliation_id -- even across processes, since this
        relies on SQLite's own file-level transaction serialization, not
        just this instance's in-process lock -- can never both succeed."""
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE reconciliation_records SET status = ?, owner = ?, claimed_at = ?, updated_at = ?, "
                    "version = version + 1 WHERE reconciliation_id = ? AND status = ?",
                    (ReconciliationStatus.RECONCILIATION_IN_PROGRESS.value, owner, now, now,
                     reconciliation_id, ReconciliationStatus.RECONCILIATION_REQUIRED.value),
                )
                conn.commit()
                return cur.rowcount == 1
            finally:
                conn.close()

    def complete(
        self, reconciliation_id: str, owner: str, *, status: ReconciliationStatus, broker_order_id: str = "",
        broker_status: str = "", filled_quantity: int = 0, average_price: float | None = None,
        rejection_reason: str = "", error_message: str = "",
    ) -> ReconciliationRecord:
        """Guarded by `owner` AND the record still being IN_PROGRESS --
        raises ReconciliationOwnershipError rather than silently overwriting
        a result someone else (or a later reclaim) already produced."""
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE reconciliation_records SET
                        status = ?, broker_order_id = COALESCE(NULLIF(?, ''), broker_order_id),
                        broker_status = ?, filled_quantity = ?, average_price = ?,
                        rejection_reason = ?, error_message = ?, completed_at = ?, updated_at = ?,
                        version = version + 1
                    WHERE reconciliation_id = ? AND owner = ? AND status = ?
                    """,
                    (
                        status.value, broker_order_id, broker_status, filled_quantity, average_price,
                        redact_string(rejection_reason), redact_string(error_message), now, now,
                        reconciliation_id, owner, ReconciliationStatus.RECONCILIATION_IN_PROGRESS.value,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        if cur.rowcount != 1:
            raise ReconciliationOwnershipError(
                f"reconciliation_id={reconciliation_id!r} could not be completed by owner={owner!r} -- "
                "it is no longer IN_PROGRESS under this owner (already completed, reclaimed, or never acquired)."
            )
        record = self.get(reconciliation_id)
        assert record is not None
        return record

    def mark_failed(self, reconciliation_id: str, owner: str, error_message: str) -> ReconciliationRecord:
        return self.complete(
            reconciliation_id, owner, status=ReconciliationStatus.RECONCILIATION_FAILED, error_message=error_message,
        )

    def reclaim_stale(self, older_than_seconds: float) -> list[str]:
        """Area M: a record stuck RECONCILIATION_IN_PROGRESS (worker crash,
        restart, timeout) becomes eligible for reconciliation again -- moved
        back to RECONCILIATION_REQUIRED with its owner cleared, NEVER reset
        to NOT_REQUIRED (which would wrongly imply nothing was ever wrong)
        and never itself submitting/resubmitting anything. Returns the
        reconciliation_ids that were reclaimed."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                stale = conn.execute(
                    "SELECT reconciliation_id FROM reconciliation_records WHERE status = ? AND claimed_at < ?",
                    (ReconciliationStatus.RECONCILIATION_IN_PROGRESS.value, cutoff),
                ).fetchall()
                ids = [r["reconciliation_id"] for r in stale]
                for rid in ids:
                    conn.execute(
                        "UPDATE reconciliation_records SET status = ?, owner = '', claimed_at = '', updated_at = ?, "
                        "version = version + 1 WHERE reconciliation_id = ? AND status = ?",
                        (ReconciliationStatus.RECONCILIATION_REQUIRED.value, now, rid,
                         ReconciliationStatus.RECONCILIATION_IN_PROGRESS.value),
                    )
                conn.commit()
            finally:
                conn.close()
        return ids

    # -- read / query (Area Q) --------------------------------------------- #
    def get(self, reconciliation_id: str) -> ReconciliationRecord | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM reconciliation_records WHERE reconciliation_id = ?", (reconciliation_id,)
                ).fetchone()
            finally:
                conn.close()
        return None if row is None else self._row_to_record(row)

    def _query(self, where: str, params: tuple) -> list[ReconciliationRecord]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT * FROM reconciliation_records WHERE {where} ORDER BY created_at ASC", params
                ).fetchall()
            finally:
                conn.close()
        return [self._row_to_record(r) for r in rows]

    def by_intent(self, correlation_id: str) -> list[ReconciliationRecord]:
        return self._query("correlation_id = ?", (correlation_id,))

    def by_idempotency_key(self, idempotency_key: str) -> ReconciliationRecord | None:
        results = self._query("idempotency_key = ?", (idempotency_key,))
        return results[0] if results else None

    def by_account(self, account_id: str) -> list[ReconciliationRecord]:
        return self._query("account_id = ?", (account_id,))

    def list_outstanding(self) -> list[ReconciliationRecord]:
        return self._query("status IN (?, ?)", _OUTSTANDING)

    def history(self, account_id: str | None = None) -> list[ReconciliationRecord]:
        if account_id is None:
            return self._query(f"status IN ({','.join('?' * len(_TERMINAL))})", tuple(_TERMINAL))
        return self._query(
            f"account_id = ? AND status IN ({','.join('?' * len(_TERMINAL))})", (account_id, *_TERMINAL)
        )


class ReadOnlyBrokerView:
    """The ONLY way ReconciliationService ever touches a BrokerClient.
    Structurally read-only: place_order/cancel_order/modify_order are not
    defined on this class at all -- there is no attribute to call, not
    merely one this code happens not to use. Every method here either
    delegates to a BrokerClient method already on the ABC (get_positions,
    get_quote, is_connected) or duck-types an OPTIONAL adapter capability
    exactly the way trading.common.execution's own pending-order
    management already does (get_order/get_order_book/get_open_orders),
    returning None when an adapter doesn't implement it rather than
    inventing a broker call that doesn't exist (Area D)."""

    def __init__(self, broker: BrokerClient) -> None:
        self._broker = broker

    def is_connected(self) -> bool:
        return self._broker.is_connected()

    def get_positions(self):
        return self._broker.get_positions()

    def get_quote(self, symbol: str):
        return self._broker.get_quote(symbol)

    def get_order(self, order_id: str):
        fn = getattr(self._broker, "get_order", None)
        return None if fn is None else fn(order_id)

    def get_order_book(self):
        fn = getattr(self._broker, "get_order_book", None)
        return None if fn is None else fn()

    def get_open_orders(self):
        fn = getattr(self._broker, "get_open_orders", None)
        return None if fn is None else fn()


def _reconstruct_expected(audit_trail: Any, idempotency_key: str) -> dict[str, Any] | None:
    """Reads the durable EVENT_ORDER_INTENT_CREATED event for this
    idempotency_key back out of the (now-persistent, Phase 15D-AUDIT)
    audit trail. Returns None if no such event exists (e.g. a record from
    before this phase's audit-event fields were added, or the audit trail
    itself doesn't support by_idempotency_key -- an in-memory
    observability.AuditTrail has no such method, which is fine: this
    subsystem simply cannot reconstruct expected data for that case and
    reports RECONCILIATION_UNKNOWN, never a guess)."""
    query = getattr(audit_trail, "by_idempotency_key", None)
    if query is None:
        return None
    for record in query(idempotency_key):
        if record.event_type == EVENT_ORDER_INTENT_CREATED:
            detail = record.detail
            return {
                "correlation_id": record.correlation_id, "strategy_id": record.strategy_id,
                "account_id": record.account_id or detail.get("account_id", ""),
                "symbol": detail.get("symbol", ""), "exchange": detail.get("exchange", ""),
                "side": detail.get("side", ""), "quantity": detail.get("quantity", 0),
                "order_type": detail.get("order_type", ""), "product_type": detail.get("product_type", ""),
                "expiry": detail.get("expiry", ""), "strike": detail.get("strike"),
                "option_type": detail.get("option_type", ""),
            }
    return None


class ReconciliationService:
    """The orchestration layer: discovers outstanding unresolved intents
    (scan_and_register) and resolves ONE of them against a real broker's
    read-only API (reconcile_one). Holds no broker-mutating capability
    whatsoever -- see ReadOnlyBrokerView above."""

    def __init__(
        self, store: SqliteReconciliationStore, idempotency_store: IdempotencyStore, audit_trail: Any,
        *, worker_id: str = "", portfolio_risk_manager: Any | None = None,
    ) -> None:
        self._store = store
        self._idempotency_store = idempotency_store
        self._audit_trail = audit_trail
        self._worker_id = worker_id or f"worker-{id(self)}"
        # Phase 17.1-R Remediation E/24 -- optional, additive, same
        # zero-behavior-change-by-default discipline as every other
        # optional dependency in trading.common (PortfolioRiskManager
        # itself, WorkerCoordinator's audit_trail/operational_alerts, ...):
        # None (the default) means reconcile_one() resolves the
        # idempotency record only, exactly as before this remediation,
        # with no portfolio-risk side effect at all.
        self._portfolio_risk = portfolio_risk_manager

    def scan_and_register(self) -> list[str]:
        """Read-only: finds every STATUS_AMBIGUOUS/STATUS_PENDING
        idempotency record and ensures a RECONCILIATION_REQUIRED record
        exists for it (idempotent -- create_required() is a no-op if one
        already exists). Safe to call repeatedly, including once per
        restart/heartbeat; never mutates a broker or the idempotency
        store itself. Returns the reconciliation_ids it ensured exist."""
        ids: list[str] = []
        for status in (STATUS_AMBIGUOUS, STATUS_PENDING):
            for idem_record in self._idempotency_store.list_by_status(status):
                expected = _reconstruct_expected(self._audit_trail, idem_record.idempotency_key)
                record = self._store.create_required(
                    idempotency_key=idem_record.idempotency_key,
                    correlation_id=(expected or {}).get("correlation_id", ""),
                    strategy_id=idem_record.strategy_id,
                    account_id=idem_record.account_id,
                    broker_id="",
                    symbol=(expected or {}).get("symbol", ""),
                    exchange=(expected or {}).get("exchange", ""),
                    expiry=(expected or {}).get("expiry", ""),
                    strike=(expected or {}).get("strike"),
                    option_type=(expected or {}).get("option_type", ""),
                    expected_side=(expected or {}).get("side", ""),
                    expected_quantity=int((expected or {}).get("quantity", 0) or 0),
                    expected_order_type=(expected or {}).get("order_type", ""),
                    expected_product_type=(expected or {}).get("product_type", ""),
                    broker_order_id=idem_record.broker_order_id,
                )
                ids.append(record.reconciliation_id)
        return ids

    def reconcile_one(self, reconciliation_id: str, broker: BrokerClient) -> ReconciliationRecord:
        """The one entry point that performs an actual (read-only) broker
        query. `broker` must be the correct, already-resolved BrokerClient
        for this record's account_id (callers resolve this exactly the way
        StrategyExecutionEngine.execute() already does, via
        BrokerManager.get_broker(account_id) -- reconciliation does not
        re-implement account routing)."""
        record = self._store.get(reconciliation_id)
        if record is None:
            raise ReconciliationPersistenceError(f"no reconciliation record for id={reconciliation_id!r}")

        if not self._store.try_acquire(reconciliation_id, owner=self._worker_id):
            # Area L: another worker already owns this (or it's already
            # terminal) -- observe, do not contend, do not duplicate work.
            return self._store.get(reconciliation_id) or record

        # -- Area C: fail CLOSED if the durable STARTED event can't be written. ---- #
        try:
            self._audit_trail.append(
                EVENT_RECONCILIATION_STARTED, correlation_id=record.correlation_id, strategy_id=record.strategy_id,
                account_id=record.account_id, idempotency_key=record.idempotency_key,
                broker_order_id=record.broker_order_id, broker_id=record.broker_id,
                reconciliation_id=reconciliation_id,
            )
        except Exception as exc:  # noqa: BLE001 -- deliberately caught: this IS the fail-closed boundary
            return self._store.mark_failed(
                reconciliation_id, self._worker_id,
                f"could not persist RECONCILIATION_STARTED -- aborting before any broker query: {exc}",
            )

        view = ReadOnlyBrokerView(broker)
        outcome = self._lookup_and_compare(record, view)

        completed = self._store.complete(
            reconciliation_id, self._worker_id, status=outcome["status"],
            broker_order_id=outcome.get("broker_order_id", record.broker_order_id),
            broker_status=outcome.get("broker_status", ""), filled_quantity=outcome.get("filled_quantity", 0),
            average_price=outcome.get("average_price"), rejection_reason=outcome.get("rejection_reason", ""),
            error_message=outcome.get("error_message", ""),
        )

        self._feed_back_to_idempotency_and_risk(record, completed)

        try:
            self._audit_trail.append(
                EVENT_RECONCILIATION_COMPLETED, correlation_id=record.correlation_id, strategy_id=record.strategy_id,
                account_id=record.account_id, idempotency_key=record.idempotency_key,
                broker_order_id=completed.broker_order_id, broker_id=record.broker_id,
                reconciliation_id=reconciliation_id, result=completed.status,
                filled_quantity=completed.filled_quantity, average_price=completed.average_price,
                rejection_reason=completed.rejection_reason, error_message=completed.error_message,
            )
        except Exception:  # noqa: BLE001
            # Area P: never silently LOSE the attempt -- the authoritative
            # answer is already durably in reconciliation_records via
            # complete() above; only the audit MIRROR of it failed to
            # write. Best-effort past this point, same precedent as
            # CentralKillSwitch's own audit call (see kill_switch.py).
            try:
                self._audit_trail.append(
                    EVENT_RECONCILIATION_FAILED, correlation_id=record.correlation_id, account_id=record.account_id,
                    idempotency_key=record.idempotency_key, reconciliation_id=reconciliation_id,
                    error_message="RECONCILIATION_COMPLETED audit write failed; result is still durable in the reconciliation store",
                )
            except Exception:  # noqa: BLE001
                pass

        return completed

    # -- Phase 17.1-R Remediation E/22/24: feed a definitive reconciliation
    # result back into the authoritative idempotency store, and (if wired)
    # into PortfolioRiskManager's held reservation -- extending this
    # existing, authoritative subsystem, never a second one. This is the
    # ONE place a reconciliation outcome is allowed to change state outside
    # reconciliation_records itself; it never places, retries, or resubmits
    # anything -- see ReadOnlyBrokerView above, unchanged. ------------------ #
    def _feed_back_to_idempotency_and_risk(self, record: ReconciliationRecord, completed: ReconciliationRecord) -> None:
        if completed.status in (
            ReconciliationStatus.RECONCILIATION_UNKNOWN.value, ReconciliationStatus.RECONCILIATION_FAILED.value,
        ):
            # REQUIRES_REVIEW-equivalent (Section 21): evidence was
            # insufficient -- HOLD. Leave the idempotency record at
            # AMBIGUOUS/PENDING and the portfolio-risk reservation open,
            # exactly as they already are. Never guess.
            return

        found = completed.status in (
            ReconciliationStatus.RECONCILED_FILLED.value, ReconciliationStatus.RECONCILED_ACCEPTED.value,
        )
        if found:
            new_status = STATUS_COMPLETED
            result = ExecutionResult(
                success=True, order_id=completed.broker_order_id, status=completed.broker_status or "COMPLETE",
                filled_quantity=completed.filled_quantity, average_price=completed.average_price,
                account_id=record.account_id, correlation_id=record.correlation_id, strategy_id=record.strategy_id,
                message="resolved by reconciliation: broker evidence confirms this order exists",
            )
        else:
            # RECONCILED_REJECTED or RECONCILED_NOT_FOUND -- Section 23's
            # explicit fail-closed policy: both become a permanent,
            # terminal REJECTED idempotency outcome. A future retry of this
            # exact idempotency_key replays this rejection and never calls
            # the broker again; it does NOT authorize a new order under a
            # different key -- that requires a distinct, explicitly
            # authorized logical action, same as any other confirmed
            # rejection (Section 23/18).
            new_status = STATUS_REJECTED
            result = ExecutionResult(
                success=False, status="REJECTED", account_id=record.account_id,
                correlation_id=record.correlation_id, strategy_id=record.strategy_id,
                message=f"resolved by reconciliation: {completed.status} ({completed.rejection_reason or completed.error_message or 'no broker evidence found'})",
            )

        resolved = self._idempotency_store.resolve_ambiguous(
            record.idempotency_key, new_status=new_status,
            broker_order_id=completed.broker_order_id, result_json=result.to_json(),
        )
        if not resolved:
            # Someone else already resolved this key (or it was never
            # AMBIGUOUS/PENDING to begin with) -- never double-resolve the
            # portfolio-risk side either; avoids exactly the "idempotency
            # says FOUND but risk reservation was released twice" hazard
            # Section 24 warns about.
            return

        if self._portfolio_risk is not None:
            reservation_id = self._portfolio_risk.find_reservation_by_idempotency_key(record.idempotency_key)
            if reservation_id is not None:
                self._portfolio_risk.resolve_reconciliation(reservation_id, found=found, fill_price=completed.average_price)

    def _lookup_and_compare(self, record: ReconciliationRecord, view: ReadOnlyBrokerView) -> dict[str, Any]:
        """Area D (lookup order) + Area E/F (expected-vs-actual, partial
        fill) + Area I/J (not-found vs unknown) + Area H (rejection).
        Returns a plain dict describing the outcome -- never raises for a
        broker-side failure (caught and reported as RECONCILIATION_UNKNOWN),
        only for a programming error."""
        try:
            state = None
            if record.broker_order_id:
                state = view.get_order(record.broker_order_id)
                if state is not None and state.status == "UNKNOWN":
                    state = None  # adapter's own "not found" sentinel (see AngelOneBroker.get_order())

            if state is None:
                # Deliberately checked with `is not None`, not a plain
                # `or` -- a genuinely empty order book ([]) is a real,
                # successful "searched, found nothing" answer and must
                # never be treated the same as "this adapter has no
                # lookup capability at all" (None), which an `x or y`
                # chain would silently do since `[]` is falsy.
                book = view.get_order_book()
                if book is None:
                    book = view.get_open_orders()
                if book is None:
                    return {
                        "status": ReconciliationStatus.RECONCILIATION_UNKNOWN,
                        "error_message": "broker adapter exposes no order-lookup capability (get_order/get_order_book/get_open_orders)",
                    }
                if len(book) == 0:
                    # Area I: never conclude "never existed" from a
                    # missing broker_order_id alone -- only from a
                    # genuine, successful search (book is not None) that
                    # came up empty.
                    return {
                        "status": ReconciliationStatus.RECONCILED_NOT_FOUND,
                        "error_message": "order not present in the broker's own order history",
                    }
                if len(book) > 1:
                    # No broker_order_id to correlate against and more
                    # than one candidate in the whole book -- cannot
                    # safely guess which one is "the" order. Symbol/side
                    # filtering is deliberately NOT applied here to narrow
                    # this down: that would risk silently picking the
                    # wrong one when a genuine mismatch is exactly what
                    # needs to be caught below, not filtered away before
                    # comparison ever runs.
                    return {
                        "status": ReconciliationStatus.RECONCILIATION_UNKNOWN,
                        "error_message": (
                            f"{len(book)} ambiguous order-book candidates and no broker_order_id to "
                            "correlate against; cannot uniquely identify the order"
                        ),
                    }
                state = book[0]
        except BrokerConnectionError as exc:
            return {"status": ReconciliationStatus.RECONCILIATION_UNKNOWN, "error_message": f"broker query failed: {exc}"}
        except Exception as exc:  # noqa: BLE001 -- any other broker-side surprise is UNKNOWN, never assumed
            return {"status": ReconciliationStatus.RECONCILIATION_UNKNOWN, "error_message": f"unexpected broker query error: {exc}"}

        # -- expected-vs-actual (Area E) ---------------------------------- #
        if state.symbol and record.symbol and state.symbol != record.symbol:
            return {
                "status": ReconciliationStatus.RECONCILIATION_FAILED,
                "broker_order_id": state.order_id, "broker_status": state.status,
                "error_message": f"symbol mismatch: expected {record.symbol!r}, broker shows {state.symbol!r}",
            }
        if state.side is not None and record.expected_side and state.side.value != record.expected_side:
            return {
                "status": ReconciliationStatus.RECONCILIATION_FAILED,
                "broker_order_id": state.order_id, "broker_status": state.status,
                "error_message": f"side mismatch: expected {record.expected_side!r}, broker shows {state.side.value!r}",
            }

        if state.status == "REJECTED":
            return {
                "status": ReconciliationStatus.RECONCILED_REJECTED, "broker_order_id": state.order_id,
                "broker_status": state.status, "rejection_reason": "broker reports order REJECTED",
            }
        if state.status == "CANCELLED":
            return {
                "status": ReconciliationStatus.RECONCILED_REJECTED, "broker_order_id": state.order_id,
                "broker_status": state.status, "rejection_reason": "broker reports order CANCELLED",
            }

        filled = state.filled_quantity
        if state.status in ("FILLED", "COMPLETE") and record.expected_quantity and filled >= record.expected_quantity:
            return {
                "status": ReconciliationStatus.RECONCILED_FILLED, "broker_order_id": state.order_id,
                "broker_status": state.status, "filled_quantity": filled,
            }

        # Area F: anything else with a real, found order -- open, or
        # filled less than expected -- is ACCEPTED (order genuinely exists
        # at the broker) but explicitly NOT treated as fully filled. The
        # actual filled_quantity is always recorded, never rounded up.
        return {
            "status": ReconciliationStatus.RECONCILED_ACCEPTED, "broker_order_id": state.order_id,
            "broker_status": state.status, "filled_quantity": filled,
        }
