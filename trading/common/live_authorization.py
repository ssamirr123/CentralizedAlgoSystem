"""
Phase 15D.5 -- a durable, explicit, single-use, exactly-scoped human
authorization for one specific live order, closing the gap every prior
live-canary phase's own report named: "no durable human-authorization
audit event". Before this module, an operator's authorization existed
only in a conversation transcript -- nothing in the system itself could
prove, durably, that a human approved this exact order, nor prevent that
approval from being silently reused for a different (or repeated) one.

--------------------------------------------------------------------------
Core principle -- exact-scope, single-use, expiring
--------------------------------------------------------------------------
A `LiveAuthorization` authorizes exactly one bounded action: a specific
account + broker + credential reference + instrument + side + quantity +
order type + product + idempotency key, within an explicit value/risk
envelope, for a short, explicit time window. It is NOT a "live trading
enabled" switch -- there is no field anywhere in this module that could
be read as "this account may now trade freely". Consuming it (via
`try_consume`) is atomic and one-shot: a second attempt against the same
authorization always fails, regardless of whether the first attempt
actually reached a broker.

This module never calls a broker and never places an order. It is a pure
gate: `StrategyExecutionEngine` (trading/common/execution.py) optionally
consults it, immediately before the broker call, exactly like the
existing idempotency `claim()` gate -- see that module's own docstring
for why "immediately before the mutating call, not earlier" matters (a
gate claimed too early poisons itself if a rejection happens upstream).

--------------------------------------------------------------------------
State machine
--------------------------------------------------------------------------
    create()                         validate()                try_consume()
  ----------->  PENDING  ------------------------------>  AUTHORIZED  --------> CONSUMED
                   |            -> REJECTED (structural             |
                   |                validation failed)              |
                   |                                                 |
                   `------------------ revoke() ---------------------+---> REVOKED
                   |                                                 |
                    `----- expires_at elapses (checked on read) -----+---> EXPIRED

CONSUMED, EXPIRED, REVOKED, and REJECTED are all terminal -- no code path
in this module transitions out of any of them. This deliberately mirrors
`TradingAccount.set_killed()`'s own irreversibility precedent.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from trading.common.audit_store import redact_string
from trading.common.deployment_info import get_deployment_info
from trading.common.file_permissions import harden_file_permissions

_DEFAULT_DB_PATH = "trading_live_authorization.db"

# Conservative default: a human-authorized canary action is meant to be
# acted on immediately, not held open. No project configuration for this
# existed before this phase; this is deliberately short and hard-coded
# rather than left unbounded.
DEFAULT_TTL_SECONDS = 10 * 60

EVENT_AUTHORIZATION_CREATED = "AUTHORIZATION_CREATED"
EVENT_AUTHORIZATION_VALIDATED = "AUTHORIZATION_VALIDATED"
EVENT_AUTHORIZATION_REJECTED = "AUTHORIZATION_REJECTED"
EVENT_AUTHORIZATION_CONSUMED = "AUTHORIZATION_CONSUMED"
EVENT_AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
EVENT_AUTHORIZATION_REVOKED = "AUTHORIZATION_REVOKED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class AuthorizationStatus(str, Enum):
    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    REJECTED = "REJECTED"


_TERMINAL = {
    AuthorizationStatus.CONSUMED.value, AuthorizationStatus.EXPIRED.value,
    AuthorizationStatus.REVOKED.value, AuthorizationStatus.REJECTED.value,
}


class LiveAuthorizationError(RuntimeError):
    """Raised by try_consume()/validate()/revoke() for any rejection --
    wrong status, expired, scope mismatch, or a genuine persistence
    failure. Never returns a value on rejection; the caller (typically
    StrategyExecutionEngine) converts this into a rejected ExecutionResult,
    exactly like AccountAuthorizationError/AmbiguousIdempotencyStateError
    are already handled."""


def mask_credential_reference(value: str) -> str:
    """Non-secret by the project's own convention (TradingAccount's own
    docstring: credential_reference "must never itself be, or contain, a
    secret value") -- masked here anyway as defense-in-depth so an audit
    record never has to be re-reviewed if that convention is ever violated
    by a future caller."""
    if not value:
        return value
    return redact_string(value) if any(
        s in value.lower() for s in ("password", "secret", "token", "totp", "mpin", "api_key")
    ) else value


@dataclass(frozen=True)
class LiveAuthorization:
    authorization_id: str
    account_id: str
    broker_id: str
    credential_reference: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    product_type: str
    max_order_value: float
    daily_loss_limit: float
    strategy_loss_limit: float
    max_orders_per_day: int
    idempotency_key: str
    authorized_by: str
    authorization_scope: str
    status: str
    rejection_reason: str
    consumed_intent_correlation_id: str
    authorized_at: str
    expires_at: str
    consumed_at: str
    revoked_at: str
    app_version: str
    git_sha: str
    deployment_id: str
    environment: str
    created_at: str
    updated_at: str
    version: int
    # Phase 15D.7 -- additive, all default to "" so every pre-existing
    # caller/row (none of which carry a real operator identity yet -- see
    # that phase's report) keeps working unchanged. `authorized_by` above
    # is UNCHANGED and still required; these fields record the specific
    # authenticated identity that field's display name was derived from.
    operator_id: str = ""
    operator_display_name: str = ""
    authentication_source: str = ""
    role: str = ""
    permission_used: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def is_expired(self, *, now: datetime | None = None) -> bool:
        now = now or _now()
        return datetime.fromisoformat(self.expires_at) <= now


def _derive_authorization_id() -> str:
    return "AUTH-" + uuid.uuid4().hex[:20]


class SqliteLiveAuthorizationStore:
    """Durable, transactional, single-use authorization state. Same
    per-call-connection + threading.Lock discipline as every other store
    in trading.common (idempotency_store.py, audit_store.py,
    reconciliation.py) -- no new persistence technology introduced."""

    def __init__(self, db_path: str | Path = _DEFAULT_DB_PATH, *, audit_trail: Any | None = None) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._audit_trail = audit_trail
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
                    CREATE TABLE IF NOT EXISTS live_authorizations (
                        authorization_id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL,
                        broker_id TEXT NOT NULL,
                        credential_reference TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        order_type TEXT NOT NULL,
                        product_type TEXT NOT NULL,
                        max_order_value REAL NOT NULL,
                        daily_loss_limit REAL NOT NULL,
                        strategy_loss_limit REAL NOT NULL,
                        max_orders_per_day INTEGER NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        authorized_by TEXT NOT NULL DEFAULT '',
                        authorization_scope TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        rejection_reason TEXT NOT NULL DEFAULT '',
                        consumed_intent_correlation_id TEXT NOT NULL DEFAULT '',
                        authorized_at TEXT NOT NULL DEFAULT '',
                        expires_at TEXT NOT NULL,
                        consumed_at TEXT NOT NULL DEFAULT '',
                        revoked_at TEXT NOT NULL DEFAULT '',
                        app_version TEXT NOT NULL DEFAULT '',
                        git_sha TEXT NOT NULL DEFAULT '',
                        deployment_id TEXT NOT NULL DEFAULT '',
                        environment TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        operator_id TEXT NOT NULL DEFAULT '',
                        operator_display_name TEXT NOT NULL DEFAULT '',
                        authentication_source TEXT NOT NULL DEFAULT '',
                        role TEXT NOT NULL DEFAULT '',
                        permission_used TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                # Phase 15D.7: additively migrate a pre-existing database
                # file that predates these columns (defensive -- no such
                # file exists in this repository outside test tmp_path
                # databases, per Phase 15D.6's own report, but a real
                # deployment could already have one).
                existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(live_authorizations)")}
                for col in ("operator_id", "operator_display_name", "authentication_source", "role", "permission_used"):
                    if col not in existing_cols:
                        conn.execute(f"ALTER TABLE live_authorizations ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
                for col in ("account_id", "status", "idempotency_key"):
                    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_liveauth_{col} ON live_authorizations({col})")
                conn.commit()
            finally:
                conn.close()

    def _row_to_record(self, row: sqlite3.Row) -> LiveAuthorization:
        return LiveAuthorization(**{k: row[k] for k in row.keys()})

    def _audit(self, event_type: str, record: LiveAuthorization, **extra: Any) -> None:
        if self._audit_trail is None:
            return
        try:
            self._audit_trail.append(
                event_type, correlation_id=record.consumed_intent_correlation_id, strategy_id="",
                account_id=record.account_id, idempotency_key=record.idempotency_key,
                authorization_id=record.authorization_id, broker_id=record.broker_id,
                credential_reference=mask_credential_reference(record.credential_reference),
                symbol=record.symbol, side=record.side, quantity=record.quantity,
                order_type=record.order_type, product_type=record.product_type,
                max_order_value=record.max_order_value, authorized_by=record.authorized_by,
                authorized_at=record.authorized_at, expires_at=record.expires_at, status=record.status,
                **extra,
            )
        except Exception:  # noqa: BLE001 -- audit must never block/crash the authorization gate itself
            pass

    # -- creation + validation -------------------------------------------- #
    def create(
        self, *, account_id: str, broker_id: str, credential_reference: str, symbol: str, side: str,
        quantity: int, order_type: str, product_type: str, max_order_value: float, daily_loss_limit: float,
        strategy_loss_limit: float, max_orders_per_day: int, idempotency_key: str, authorized_by: str,
        authorization_scope: str = "", ttl_seconds: int = DEFAULT_TTL_SECONDS,
        operator_id: str = "", operator_display_name: str = "", authentication_source: str = "",
        role: str = "", permission_used: str = "",
    ) -> LiveAuthorization:
        if not idempotency_key:
            raise LiveAuthorizationError("idempotency_key is required to create a live authorization")
        authorization_id = _derive_authorization_id()
        now = _now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        info = get_deployment_info()
        now_s = _iso(now)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO live_authorizations
                        (authorization_id, account_id, broker_id, credential_reference, symbol, side, quantity,
                         order_type, product_type, max_order_value, daily_loss_limit, strategy_loss_limit,
                         max_orders_per_day, idempotency_key, authorized_by, authorization_scope, status,
                         authorized_at, expires_at, app_version, git_sha, deployment_id, environment,
                         created_at, updated_at, version, operator_id, operator_display_name,
                         authentication_source, role, permission_used)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        authorization_id, account_id, broker_id, credential_reference, symbol, side, quantity,
                        order_type, product_type, max_order_value, daily_loss_limit, strategy_loss_limit,
                        max_orders_per_day, idempotency_key, authorized_by, authorization_scope,
                        AuthorizationStatus.PENDING.value, "", _iso(expires_at),
                        info.app_version, info.git_sha, info.deployment_id, info.environment, now_s, now_s,
                        operator_id, operator_display_name, authentication_source, role, permission_used,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise LiveAuthorizationError(f"idempotency_key {idempotency_key!r} already has an authorization: {exc}") from exc
            except sqlite3.OperationalError as exc:
                conn.rollback()
                raise LiveAuthorizationError(f"live authorization database unavailable/locked: {exc}") from exc
            finally:
                conn.close()
        record = self.get(authorization_id)
        assert record is not None
        self._audit(EVENT_AUTHORIZATION_CREATED, record)
        return record

    def validate(self, authorization_id: str) -> LiveAuthorization:
        """Structural validation only (required fields present, positive
        quantity/value, expiry still in the future) -- transitions
        PENDING -> AUTHORIZED or PENDING -> REJECTED. This is deliberately
        NOT where exact-scope-vs-intent matching happens (that is
        try_consume()'s job, against the real OrderIntent, which does not
        exist yet at creation time)."""
        record = self.get(authorization_id)
        if record is None:
            raise LiveAuthorizationError(f"no such authorization_id={authorization_id!r}")
        if record.status != AuthorizationStatus.PENDING.value:
            raise LiveAuthorizationError(f"authorization {authorization_id!r} is {record.status}, not PENDING")

        reason = ""
        if record.is_expired():
            reason = "expires_at is already in the past"
        elif record.quantity <= 0:
            reason = "quantity must be positive"
        elif record.max_order_value <= 0:
            reason = "max_order_value must be positive"
        elif not record.symbol or not record.side or not record.order_type or not record.product_type:
            reason = "instrument/side/order_type/product_type must all be set"
        elif not record.account_id or not record.broker_id or not record.credential_reference:
            reason = "account_id/broker_id/credential_reference must all be set"

        new_status = AuthorizationStatus.REJECTED if reason else AuthorizationStatus.AUTHORIZED
        updated = self._transition(
            authorization_id, from_status=AuthorizationStatus.PENDING.value, to_status=new_status.value,
            rejection_reason=reason,
        )
        self._audit(
            EVENT_AUTHORIZATION_REJECTED if reason else EVENT_AUTHORIZATION_VALIDATED, updated,
            rejection_reason=reason,
        )
        if reason:
            raise LiveAuthorizationError(f"authorization {authorization_id!r} rejected: {reason}")
        return updated

    # -- single-use consumption (the actual gate) -------------------------- #
    def try_consume(
        self, authorization_id: str, *, account_id: str, broker_id: str, credential_reference: str,
        symbol: str, side: str, quantity: int, order_type: str, product_type: str, idempotency_key: str,
        order_value: float, correlation_id: str = "", operator_id: str | None = None,
    ) -> LiveAuthorization:
        """The one and only place a LiveAuthorization is actually spent.
        Atomic: guarded by (authorization_id, status='AUTHORIZED') in the
        UPDATE's WHERE clause, so two concurrent callers can never both
        succeed -- exactly the same compare-and-swap pattern as
        SqliteIdempotencyStore.claim()/SqliteReconciliationStore.try_acquire().

        Fails closed (raises LiveAuthorizationError, consumes nothing) for
        ANY of: wrong/terminal status, expiry, or any exact-scope mismatch
        against the fields of the OrderIntent actually about to be
        submitted."""
        record = self.get(authorization_id)
        if record is None:
            raise LiveAuthorizationError(f"no such authorization_id={authorization_id!r}")
        if record.status != AuthorizationStatus.AUTHORIZED.value:
            raise LiveAuthorizationError(
                f"authorization {authorization_id!r} is {record.status}, not AUTHORIZED -- cannot consume"
            )

        mismatches = []
        for field_name, expected, actual in (
            ("account_id", record.account_id, account_id),
            ("broker_id", record.broker_id, broker_id),
            ("credential_reference", record.credential_reference, credential_reference),
            ("symbol", record.symbol, symbol),
            ("side", record.side, side),
            ("quantity", record.quantity, quantity),
            ("order_type", record.order_type, order_type),
            ("product_type", record.product_type, product_type),
            ("idempotency_key", record.idempotency_key, idempotency_key),
        ):
            if expected != actual:
                mismatches.append(f"{field_name}: authorized={expected!r} actual={actual!r}")
        # Phase 15D.7: operator_id is only compared when the CALLER
        # supplies one (not None) -- omitting it entirely preserves every
        # pre-15D.7 caller's exact behavior (this project has no record
        # anywhere, including production, with a real operator_id set
        # before this phase). Once a caller does pass it, a mismatch
        # (including "" vs a real id) is rejected exactly like every other
        # scope field: a different operator can never replay or consume
        # another operator's authorization.
        if operator_id is not None and record.operator_id != operator_id:
            mismatches.append(f"operator_id: authorized={record.operator_id!r} actual={operator_id!r}")
        if order_value > record.max_order_value:
            mismatches.append(f"order_value {order_value} exceeds authorized max_order_value {record.max_order_value}")

        if mismatches:
            raise LiveAuthorizationError(
                f"authorization {authorization_id!r} does not match the intended action: {'; '.join(mismatches)}"
            )

        now = _now()
        if record.is_expired(now=now):
            expired = self._transition(
                authorization_id, from_status=AuthorizationStatus.AUTHORIZED.value,
                to_status=AuthorizationStatus.EXPIRED.value,
            )
            self._audit(EVENT_AUTHORIZATION_EXPIRED, expired)
            raise LiveAuthorizationError(f"authorization {authorization_id!r} expired at {record.expires_at}")

        now_s = _iso(now)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE live_authorizations SET status = ?, consumed_at = ?, updated_at = ?,
                        consumed_intent_correlation_id = ?, version = version + 1
                    WHERE authorization_id = ? AND status = ?
                    """,
                    (
                        AuthorizationStatus.CONSUMED.value, now_s, now_s, correlation_id,
                        authorization_id, AuthorizationStatus.AUTHORIZED.value,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        if cur.rowcount != 1:
            raise LiveAuthorizationError(
                f"authorization {authorization_id!r} could not be consumed -- concurrently consumed, "
                "expired, or revoked between validation and this attempt"
            )
        record = self.get(authorization_id)
        assert record is not None
        self._audit(EVENT_AUTHORIZATION_CONSUMED, record)
        return record

    def revoke(self, authorization_id: str, *, reason: str = "", by: str = "") -> LiveAuthorization:
        record = self.get(authorization_id)
        if record is None:
            raise LiveAuthorizationError(f"no such authorization_id={authorization_id!r}")
        if record.is_terminal:
            raise LiveAuthorizationError(f"authorization {authorization_id!r} is already {record.status} -- cannot revoke")
        now_s = _iso(_now())
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    """
                    UPDATE live_authorizations SET status = ?, revoked_at = ?, updated_at = ?,
                        rejection_reason = ?, version = version + 1
                    WHERE authorization_id = ? AND status IN (?, ?)
                    """,
                    (
                        AuthorizationStatus.REVOKED.value, now_s, now_s, redact_string(reason),
                        authorization_id, AuthorizationStatus.PENDING.value, AuthorizationStatus.AUTHORIZED.value,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        if cur.rowcount != 1:
            raise LiveAuthorizationError(f"authorization {authorization_id!r} could not be revoked (concurrent state change)")
        record = self.get(authorization_id)
        assert record is not None
        self._audit(EVENT_AUTHORIZATION_REVOKED, record, reason=redact_string(reason), by=by)
        return record

    def _transition(self, authorization_id: str, *, from_status: str, to_status: str, **extra_fields: Any) -> LiveAuthorization:
        now_s = _iso(_now())
        set_clauses = ["status = ?", "updated_at = ?", "version = version + 1"]
        params: list[Any] = [to_status, now_s]
        for k, v in extra_fields.items():
            set_clauses.append(f"{k} = ?")
            params.append(v)
        params.extend([authorization_id, from_status])
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    f"UPDATE live_authorizations SET {', '.join(set_clauses)} WHERE authorization_id = ? AND status = ?",
                    params,
                )
                conn.commit()
            finally:
                conn.close()
        if cur.rowcount != 1:
            raise LiveAuthorizationError(f"authorization {authorization_id!r} was not in status {from_status!r}")
        record = self.get(authorization_id)
        assert record is not None
        return record

    # -- read / query -------------------------------------------------- #
    def get(self, authorization_id: str) -> LiveAuthorization | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM live_authorizations WHERE authorization_id = ?", (authorization_id,)
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        record = self._row_to_record(row)
        # Lazily, durably transition an overdue PENDING/AUTHORIZED record to
        # EXPIRED on read -- see module docstring: expiry must be durable,
        # not merely computed on the fly, so it survives restart identically.
        if record.status in (AuthorizationStatus.PENDING.value, AuthorizationStatus.AUTHORIZED.value) and record.is_expired():
            record = self._transition(
                authorization_id, from_status=record.status, to_status=AuthorizationStatus.EXPIRED.value,
            )
            self._audit(EVENT_AUTHORIZATION_EXPIRED, record)
        return record

    def by_idempotency_key(self, idempotency_key: str) -> LiveAuthorization | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT authorization_id FROM live_authorizations WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
            finally:
                conn.close()
        return None if row is None else self.get(row["authorization_id"])

    def by_account(self, account_id: str) -> list[LiveAuthorization]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT authorization_id FROM live_authorizations WHERE account_id = ? ORDER BY created_at ASC",
                    (account_id,),
                ).fetchall()
            finally:
                conn.close()
        return [self.get(r["authorization_id"]) for r in rows]
