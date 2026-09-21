"""
Phase 15D-AUDIT -- a durable, hash-chained, append-only audit trail that
survives process restart, deployment, and crash.

Phase 13's `trading.common.observability.AuditTrail` is tamper-EVIDENT (a
hash chain detects modification) but explicitly in-memory only -- see its
own module docstring. Every event vanishes on restart. That is the single
gap this module closes, without touching AuditTrail itself (Rule 1 of this
phase's brief: do not rewrite working safety logic unnecessarily) -- this
is a new, additive, SQLite-backed store plus a thin `PersistentAuditTrail`
wrapper that is a drop-in replacement anywhere an `AuditTrail` is accepted,
because every caller in this codebase (StrategyExecutionEngine, BrokerManager,
AlertManager, the strategy classes) only ever calls `.append(event_type,
*, correlation_id=, strategy_id=, **detail)` -- never `isinstance()` --
so nothing else needs to change to adopt it.

--------------------------------------------------------------------------
Design
--------------------------------------------------------------------------
Single source of truth = the SQLite file. There is no separate in-memory
list that could ever diverge from what's on disk: `.records()`, `.trace()`,
and every `by_*` query read straight from the database, and `.verify()`
recomputes the hash chain from what's actually stored -- so "the audit
trail" and "what a fresh process reads after restart" are, by construction,
the same thing.

Every event automatically carries the process's own deployment identity
(app_version/git_sha/deployment_id/environment, from
trading.common.deployment_info) -- callers never need to pass these.

Immutability is enforced at the database level, not just by Python
discipline: `CREATE TRIGGER ... RAISE(ABORT, ...)` on both UPDATE and
DELETE against the events table means even a stray direct SQL statement
against this file cannot silently rewrite or remove a past record --
only INSERT is possible. A correction is always a NEW event referencing
the original by `corrects_event_id` in its own detail, never a rewrite.

Fail-closed, deliberately: append() either fully commits (record is on
disk, hash-chained to everything before it) or raises AuditPersistenceError
-- there is no third outcome. In particular this module contains NO
"database unavailable -> fall back to an in-memory buffer and pretend it
worked" path; Area D of this phase's brief explicitly forbids exactly that
shape of fallback. What happens when execute() -- Phase 14.6 Blocker C --
calls this through ObservabilityHealth.safe_observe() is a DIFFERENT,
already-settled decision from an earlier phase: an audit-write failure must
never itself cause a duplicate broker order by crashing execute() after a
broker call already went out, so safe_observe() swallows the exception
there. That does not conflict with "fail closed" here: this store still
never fabricates success, and ObservabilityHealth.failures()/`.healthy`
remain the visible signal that a safety-critical write did not durably
happen -- see docs/phase-15d-audit-persistence-report.md for the full
reasoning.

Not built here (explicit, documented scope boundary, matching Phase
15D-DR's own "no infrastructure beyond what's needed" instruction): a
PostgreSQL backend. The `AuditStore`-shaped surface (append/records/by_*)
is the seam a future implementation would sit behind; nothing in
PersistentAuditTrail or its callers assumes SQLite specifically beyond the
constructor.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from trading.common.deployment_info import get_deployment_info
from trading.common.file_permissions import harden_file_permissions

_DEFAULT_DB_PATH = "trading_audit.db"

# Deliberately generous but bounded -- Area N (oversized event): a caller
# error (e.g. accidentally attaching a full broker response object) must
# be rejected loudly, not silently truncated (which would corrupt the hash
# chain's own reproducibility) and not allowed to grow the file unbounded.
MAX_DETAIL_BYTES = 64 * 1024

# Substrings (case-insensitive) that, if found in a detail key, cause that
# key's VALUE to be replaced with a fixed redaction marker before the event
# is ever hashed or written to disk. Matches the module docstring's "do not
# persist credentials/API keys/TOTP secrets/passwords/MPINs/access tokens"
# requirement as defense-in-depth -- callers should never pass these in the
# first place (see trading/common/trading_account.py's own Security note),
# but a bug at a call site must not become a durable secret leak.
#
# Deliberately does NOT include the bare substring "credential": this
# codebase has a real, non-secret field of that shape --
# TradingAccount.credential_reference, documented on that class as "a
# NON-secret pointer to where credentials live... must never itself be, or
# contain, a secret value" -- and a generic "credential*" match would
# wrongly redact it (and any dict literally named "credentials" used only
# as a structural container, whose own leaf fields are redacted on their
# own merits below regardless of the container's name).
_SECRET_KEY_SUBSTRINGS = (
    "password", "passwd", "secret", "api_key", "apikey", "access_token",
    "refresh_token", "token", "totp", "mpin", "private_key", "auth_code",
)
REDACTED = "***REDACTED***"

# --------------------------------------------------------------------------
# Event-type constants for the safety-critical events this phase's brief
# (Area D) names, beyond what trading.common.observability already defines.
# Some of these (kill-switch, authorization-state-change, deployment
# startup/shutdown) have a real call site wired in this phase. Others
# (reconciliation, human-authorization) do not yet have a producing
# subsystem in this codebase -- defined here so the schema/query surface is
# ready, and exercised directly by tests, but honestly NOT claimed as
# "wired into production" until a reconciliation/human-authorization module
# exists to call them. See docs/phase-15d-audit-persistence-report.md.
# --------------------------------------------------------------------------
EVENT_KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
EVENT_KILL_SWITCH_DISENGAGED = "KILL_SWITCH_DISENGAGED"
EVENT_AUTHORIZATION_STATE_CHANGED = "AUTHORIZATION_STATE_CHANGED"
EVENT_DEPLOYMENT_STARTUP = "DEPLOYMENT_STARTUP"
EVENT_DEPLOYMENT_SHUTDOWN = "DEPLOYMENT_SHUTDOWN"
EVENT_RECONCILIATION_STARTED = "RECONCILIATION_STARTED"
EVENT_RECONCILIATION_COMPLETED = "RECONCILIATION_COMPLETED"
# Phase 15D-RECON: reconciliation itself (not the underlying broker
# order) failed to reach a conclusion -- e.g. the durable STARTED event
# could not be written (fail-closed, Area C) or the read-only broker
# query raised. Distinct from a definitive RECONCILED_REJECTED finding.
EVENT_RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
EVENT_HUMAN_AUTHORIZATION_REQUESTED = "HUMAN_AUTHORIZATION_REQUESTED"
EVENT_HUMAN_AUTHORIZATION_DECISION = "HUMAN_AUTHORIZATION_DECISION"
# Matches the exact literal string trading.common.execution._handle_ambiguous()
# already appends via the in-memory AuditTrail -- named here for grep-ability
# and so tests/callers of PersistentAuditTrail don't need to hardcode it.
EVENT_AMBIGUOUS_ORDER_STATE = "AMBIGUOUS_ORDER_STATE"

_INDEXED_DETAIL_KEYS = ("account_id", "owner_id", "broker_id", "idempotency_key", "broker_order_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditPersistenceError(RuntimeError):
    """Raised whenever a safety-critical audit write could not be durably
    committed -- a locked/unavailable database, a malformed event, an
    oversized detail payload, or any other failure. Never swallowed inside
    this module: the caller (or, in StrategyExecutionEngine's case,
    ObservabilityHealth.safe_observe()) decides how to surface it. This
    module itself never returns a success value when the write did not
    actually happen -- see the module docstring's Fail-closed section."""


class AuditImmutabilityError(RuntimeError):
    """Raised when an UPDATE or DELETE against the audit_events table is
    attempted and rejected by the database's own append-only trigger --
    i.e. immutability was enforced at the storage layer itself, not merely
    by Python-level discipline."""


def _redact(detail: dict[str, Any]) -> dict[str, Any]:
    def _redact_value(key: str, value: Any) -> Any:
        lowered = key.lower()
        if any(s in lowered for s in _SECRET_KEY_SUBSTRINGS):
            return REDACTED
        if isinstance(value, dict):
            return {k: _redact_value(k, v) for k, v in value.items()}
        return value

    return {k: _redact_value(k, v) for k, v in detail.items()}


def redact_string(text: str) -> str:
    """Phase 15D-RECON: `_redact()` above only catches secrets carried
    under a secret-SHAPED KEY in a detail dict -- it can't help a free-text
    VALUE (e.g. an error message) that happens to embed something secret-
    looking (a broker/SDK exception occasionally echoes back part of the
    request it failed on). This is the value-side complement: if the text
    itself contains one of the same secret-shaped substrings, the whole
    string is replaced -- blunt, but never allows a partial leak through
    a message no caller explicitly reviewed. Used by
    trading.common.reconciliation for error_message/rejection_reason
    fields before they are persisted or audited."""
    if not text:
        return text
    lowered = text.lower()
    if any(s in lowered for s in _SECRET_KEY_SUBSTRINGS):
        return REDACTED
    return text


@dataclass(frozen=True)
class PersistedAuditRecord:
    event_id: str
    seq: int
    timestamp: str
    event_type: str
    correlation_id: str
    strategy_id: str
    account_id: str
    owner_id: str
    broker_id: str
    idempotency_key: str
    broker_order_id: str
    deployment_id: str
    app_version: str
    git_sha: str
    environment: str
    detail: dict[str, Any]
    prev_hash: str
    hash: str


def _payload_for_hash(row: dict[str, Any]) -> str:
    """The exact, deterministic byte sequence a record's hash commits to --
    same field set every time, sorted keys, so `hash` is a pure function of
    (seq, all indexed fields, detail, prev_hash). This is what makes
    `event_id` (== `hash`) deterministic: replaying the exact same append
    at the exact same chain position always yields the exact same id."""
    return json.dumps(
        {
            "seq": row["seq"], "timestamp": row["timestamp"], "event_type": row["event_type"],
            "correlation_id": row["correlation_id"], "strategy_id": row["strategy_id"],
            "account_id": row["account_id"], "owner_id": row["owner_id"], "broker_id": row["broker_id"],
            "idempotency_key": row["idempotency_key"], "broker_order_id": row["broker_order_id"],
            "deployment_id": row["deployment_id"], "app_version": row["app_version"],
            "git_sha": row["git_sha"], "environment": row["environment"],
            "detail": row["detail"], "prev_hash": row["prev_hash"],
        },
        sort_keys=True, default=str,
    )


class SqliteAuditStore:
    """Durable, transactional, append-only backing store. Standard-library
    sqlite3 only -- same layering discipline as
    trading.common.idempotency_store.SqliteIdempotencyStore (trading.common
    stays free of any concrete database dependency beyond the stdlib)."""

    GENESIS_HASH = "0" * 64

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
                    CREATE TABLE IF NOT EXISTS audit_events (
                        event_id TEXT PRIMARY KEY,
                        seq INTEGER NOT NULL UNIQUE,
                        timestamp TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        correlation_id TEXT NOT NULL DEFAULT '',
                        strategy_id TEXT NOT NULL DEFAULT '',
                        account_id TEXT NOT NULL DEFAULT '',
                        owner_id TEXT NOT NULL DEFAULT '',
                        broker_id TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        broker_order_id TEXT NOT NULL DEFAULT '',
                        deployment_id TEXT NOT NULL DEFAULT '',
                        app_version TEXT NOT NULL DEFAULT '',
                        git_sha TEXT NOT NULL DEFAULT '',
                        environment TEXT NOT NULL DEFAULT '',
                        detail_json TEXT NOT NULL,
                        prev_hash TEXT NOT NULL,
                        hash TEXT NOT NULL
                    )
                    """
                )
                for col in ("correlation_id", "strategy_id", "account_id", "idempotency_key",
                            "broker_order_id", "event_type", "deployment_id", "timestamp"):
                    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_audit_{col} ON audit_events({col})")
                # Append-only, enforced by the database itself -- not merely
                # by every caller happening to only ever call INSERT.
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                    BEFORE UPDATE ON audit_events
                    BEGIN
                        SELECT RAISE(ABORT, 'audit_events is append-only: UPDATE is not permitted');
                    END
                    """
                )
                conn.execute(
                    """
                    CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
                    BEFORE DELETE ON audit_events
                    BEGIN
                        SELECT RAISE(ABORT, 'audit_events is append-only: DELETE is not permitted');
                    END
                    """
                )
                conn.commit()
            finally:
                conn.close()

    # -- write ---------------------------------------------------------- #
    def append(
        self, event_type: str, *, correlation_id: str = "", strategy_id: str = "",
        account_id: str = "", owner_id: str = "", broker_id: str = "",
        idempotency_key: str = "", broker_order_id: str = "", detail: dict[str, Any] | None = None,
    ) -> PersistedAuditRecord:
        if not event_type or not isinstance(event_type, str):
            raise AuditPersistenceError(f"event_type must be a non-empty string, got {event_type!r}")
        for name, value in (("account_id", account_id), ("owner_id", owner_id), ("broker_id", broker_id),
                            ("idempotency_key", idempotency_key), ("broker_order_id", broker_order_id),
                            ("correlation_id", correlation_id), ("strategy_id", strategy_id)):
            if not isinstance(value, str):
                raise AuditPersistenceError(f"{name} must be a string, got {type(value).__name__}")

        safe_detail = _redact(dict(detail or {}))
        try:
            # No `default=str` fallback here, deliberately: a call site
            # passing a non-JSON-safe value (e.g. accidentally attaching a
            # live object) is a bug worth failing loudly on (Area N), not
            # silently coercing to str(obj) and hashing whatever that
            # happens to produce.
            detail_json = json.dumps(safe_detail, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise AuditPersistenceError(f"audit event detail is not JSON-serializable: {exc}") from exc
        if len(detail_json.encode("utf-8")) > MAX_DETAIL_BYTES:
            raise AuditPersistenceError(
                f"audit event detail is {len(detail_json.encode('utf-8'))} bytes, exceeding the "
                f"{MAX_DETAIL_BYTES}-byte limit -- refusing to write a partial/truncated event."
            )

        info = get_deployment_info()
        timestamp = _now()

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                last = conn.execute(
                    "SELECT seq, hash FROM audit_events ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                seq = 0 if last is None else last["seq"] + 1
                prev_hash = self.GENESIS_HASH if last is None else last["hash"]

                row = {
                    "seq": seq, "timestamp": timestamp, "event_type": event_type,
                    "correlation_id": correlation_id, "strategy_id": strategy_id,
                    "account_id": account_id, "owner_id": owner_id, "broker_id": broker_id,
                    "idempotency_key": idempotency_key, "broker_order_id": broker_order_id,
                    "deployment_id": info.deployment_id, "app_version": info.app_version,
                    "git_sha": info.git_sha, "environment": info.environment,
                    "detail": safe_detail, "prev_hash": prev_hash,
                }
                digest = sha256(_payload_for_hash(row).encode("utf-8")).hexdigest()
                event_id = digest  # deterministic: pure function of chain position + content

                conn.execute(
                    """
                    INSERT INTO audit_events
                        (event_id, seq, timestamp, event_type, correlation_id, strategy_id,
                         account_id, owner_id, broker_id, idempotency_key, broker_order_id,
                         deployment_id, app_version, git_sha, environment, detail_json, prev_hash, hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id, seq, timestamp, event_type, correlation_id, strategy_id,
                        account_id, owner_id, broker_id, idempotency_key, broker_order_id,
                        info.deployment_id, info.app_version, info.git_sha, info.environment,
                        detail_json, prev_hash, digest,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise AuditPersistenceError(f"duplicate audit event rejected: {exc}") from exc
            except sqlite3.OperationalError as exc:
                conn.rollback()
                raise AuditPersistenceError(f"audit database unavailable/locked: {exc}") from exc
            finally:
                conn.close()

        return PersistedAuditRecord(
            event_id=event_id, seq=seq, timestamp=timestamp, event_type=event_type,
            correlation_id=correlation_id, strategy_id=strategy_id, account_id=account_id,
            owner_id=owner_id, broker_id=broker_id, idempotency_key=idempotency_key,
            broker_order_id=broker_order_id, deployment_id=info.deployment_id,
            app_version=info.app_version, git_sha=info.git_sha, environment=info.environment,
            detail=safe_detail, prev_hash=prev_hash, hash=digest,
        )

    # -- read / query ----------------------------------------------------- #
    def _row_to_record(self, row: sqlite3.Row) -> PersistedAuditRecord:
        try:
            detail = json.loads(row["detail_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise AuditPersistenceError(
                f"audit event {row['event_id']} has corrupted detail_json -- refusing to silently "
                f"skip or fabricate a value: {exc}"
            ) from exc
        return PersistedAuditRecord(
            event_id=row["event_id"], seq=row["seq"], timestamp=row["timestamp"], event_type=row["event_type"],
            correlation_id=row["correlation_id"], strategy_id=row["strategy_id"], account_id=row["account_id"],
            owner_id=row["owner_id"], broker_id=row["broker_id"], idempotency_key=row["idempotency_key"],
            broker_order_id=row["broker_order_id"], deployment_id=row["deployment_id"],
            app_version=row["app_version"], git_sha=row["git_sha"], environment=row["environment"],
            detail=detail, prev_hash=row["prev_hash"], hash=row["hash"],
        )

    def _query(self, where: str, params: tuple) -> list[PersistedAuditRecord]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT * FROM audit_events WHERE {where} ORDER BY seq ASC", params
                ).fetchall()
            finally:
                conn.close()
        return [self._row_to_record(r) for r in rows]

    def records(self) -> list[PersistedAuditRecord]:
        return self._query("1=1", ())

    def by_account(self, account_id: str) -> list[PersistedAuditRecord]:
        return self._query("account_id = ?", (account_id,))

    def by_intent(self, correlation_id: str) -> list[PersistedAuditRecord]:
        return self._query("correlation_id = ?", (correlation_id,))

    def by_idempotency_key(self, idempotency_key: str) -> list[PersistedAuditRecord]:
        return self._query("idempotency_key = ?", (idempotency_key,))

    def by_broker_order_id(self, broker_order_id: str) -> list[PersistedAuditRecord]:
        return self._query("broker_order_id = ?", (broker_order_id,))

    def by_time_range(self, start_iso: str, end_iso: str) -> list[PersistedAuditRecord]:
        return self._query("timestamp >= ? AND timestamp <= ?", (start_iso, end_iso))

    def by_event_type(self, event_type: str) -> list[PersistedAuditRecord]:
        return self._query("event_type = ?", (event_type,))

    def by_deployment_id(self, deployment_id: str) -> list[PersistedAuditRecord]:
        return self._query("deployment_id = ?", (deployment_id,))

    def verify_chain(self) -> bool:
        """Recompute every hash from scratch, exactly like
        observability.AuditTrail.verify() -- detects any modification,
        reordering, or deletion. Because the database itself rejects
        UPDATE/DELETE (see the triggers created in _init_schema()), the
        only way this can ever return False is a lower-level bypass of
        SQLite's own constraints (e.g. hand-editing the file with a
        different tool) -- i.e. genuine tamper detection, not a
        self-fulfilling check against data this same class also wrote
        under normal operation."""
        prev_hash = self.GENESIS_HASH
        for r in self.records():
            row = {
                "seq": r.seq, "timestamp": r.timestamp, "event_type": r.event_type,
                "correlation_id": r.correlation_id, "strategy_id": r.strategy_id,
                "account_id": r.account_id, "owner_id": r.owner_id, "broker_id": r.broker_id,
                "idempotency_key": r.idempotency_key, "broker_order_id": r.broker_order_id,
                "deployment_id": r.deployment_id, "app_version": r.app_version,
                "git_sha": r.git_sha, "environment": r.environment,
                "detail": r.detail, "prev_hash": prev_hash,
            }
            expected_hash = sha256(_payload_for_hash(row).encode("utf-8")).hexdigest()
            if r.prev_hash != prev_hash or r.hash != expected_hash:
                return False
            prev_hash = r.hash
        return True


class PersistentAuditTrail:
    """Drop-in replacement for trading.common.observability.AuditTrail
    anywhere an `audit_trail=` is accepted (StrategyExecutionEngine,
    BrokerManager, AlertManager, every strategy class) -- every such
    caller only ever calls `.append(event_type, *, correlation_id=,
    strategy_id=, **detail)`, which this class implements with the exact
    same signature, so no other module needs to change to adopt durability.

    `**detail` may include `account_id`/`owner_id`/`broker_id`/
    `idempotency_key`/`broker_order_id` (as most call sites already pass,
    e.g. execution.py's EVENT_AUTHORIZATION_STATE_GATE/BROKER_ORDER_PLACED
    events) -- when present, these are promoted to indexed columns for the
    by_*() queries below; everything else stays in the JSON detail blob.
    """

    def __init__(self, db_path: str | Path = _DEFAULT_DB_PATH) -> None:
        self._store = SqliteAuditStore(db_path)

    def append(self, event_type: str, *, correlation_id: str = "", strategy_id: str = "", **detail: Any) -> PersistedAuditRecord:
        promoted = {k: str(detail.pop(k)) for k in list(_INDEXED_DETAIL_KEYS) if k in detail}
        return self._store.append(
            event_type, correlation_id=correlation_id, strategy_id=strategy_id, detail=detail, **promoted
        )

    def records(self) -> list[PersistedAuditRecord]:
        return self._store.records()

    def trace(self, correlation_id: str) -> list[PersistedAuditRecord]:
        return self._store.by_intent(correlation_id)

    def by_account(self, account_id: str) -> list[PersistedAuditRecord]:
        return self._store.by_account(account_id)

    def by_intent(self, correlation_id: str) -> list[PersistedAuditRecord]:
        return self._store.by_intent(correlation_id)

    def by_idempotency_key(self, idempotency_key: str) -> list[PersistedAuditRecord]:
        return self._store.by_idempotency_key(idempotency_key)

    def by_broker_order_id(self, broker_order_id: str) -> list[PersistedAuditRecord]:
        return self._store.by_broker_order_id(broker_order_id)

    def by_time_range(self, start_iso: str, end_iso: str) -> list[PersistedAuditRecord]:
        return self._store.by_time_range(start_iso, end_iso)

    def by_event_type(self, event_type: str) -> list[PersistedAuditRecord]:
        return self._store.by_event_type(event_type)

    def by_deployment_id(self, deployment_id: str) -> list[PersistedAuditRecord]:
        return self._store.by_deployment_id(deployment_id)

    def verify(self) -> bool:
        return self._store.verify_chain()

    def verify_or_raise(self) -> None:
        if not self.verify():
            from trading.common.observability import TamperDetectedError

            raise TamperDetectedError(
                "Persistent audit trail hash chain verification failed -- a record was modified, "
                "reordered, or deleted."
            )


def record_authorization_transition(
    audit_trail: Any, account: Any, previous_state: str, reason: str = "",
) -> None:
    """Phase 15D-AUDIT (Area K): audits a TradingAccount authorization_state
    transition. Deliberately a free function taking the trail as a plain
    argument, rather than a method on TradingAccount or a hook baked into
    it -- TradingAccount stays free of any observability dependency (see
    its own module docstring's layering), and this makes explicit, in one
    place, the boundary Area K insists on: "the audit system must never
    itself grant authorization... audit logging is observational". Calling
    this function has zero effect on `account.authorization_state` --
    the transition must already have happened (e.g. via
    TradingAccount.set_authorization_state()) before this is called to
    record it."""
    if audit_trail is None:
        return
    audit_trail.append(
        EVENT_AUTHORIZATION_STATE_CHANGED,
        correlation_id="", strategy_id="", account_id=account.account_id,
        owner_id=getattr(account, "owner_id", ""), previous_state=previous_state,
        new_state=account.authorization_state.value, reason=reason,
    )
