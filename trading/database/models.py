"""
Canonical application schema (single SQLAlchemy Base).

Control-center tables per the project's own design: servers, algos,
algo_runs, heartbeats, logs, positions, trades, daily_pnl, commands,
rate_limit_windows.

Dormant table: strategy_heartbeats -- the original per-(strategy, server)
latest-state row. The endpoints that fed it (POST /update_strategy,
GET /strategies) have been removed; the table is retained unchanged so
its historical production rows are preserved. Nothing writes to it now.

Naming note: Milestone 4's Lambda (orchestrator.py) uses "algo_id" in its
event payload to mean the algo's NAME (e.g. "example_strategy"), matching
trading_agent.py's CLI convention -- not this schema's algos.id integer
PK. Externally (SSM, Lambda, dashboard), an algo is identified by name;
internally, algos.id is the relational FK target everywhere below.

All timestamps are stored timezone-aware in UTC. Converting to IST is a
display-layer concern for the dashboard (Milestone 7), not this schema's.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trading.database.connection import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    ec2_instance_id: Mapped[str] = mapped_column(String(32), nullable=False)
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="UNKNOWN")
    # "windows" or "linux" -- selects the SSM document (AWS-RunPowerShellScript
    # vs AWS-RunShellScript) and command syntax for every action the
    # orchestrator Lambda sends to this specific server, matching
    # ssm_invoke.py's existing --os pattern instead of the Lambda's old
    # single hardcoded target.
    os: Mapped[str] = mapped_column(String(20), nullable=False, default="linux")
    repo_path: Mapped[str] = mapped_column(String(255), nullable=False, default="/trading-app")
    # PROVISIONING while the async bootstrap Lambda action is attaching
    # the IAM profile / rebooting / waiting for SSM / cloning / installing
    # deps; READY once it can actually run algo commands; FAILED with
    # provisioning_message explaining why if any step didn't complete.
    provisioning_status: Mapped[str] = mapped_column(String(20), nullable=False, default="READY")
    provisioning_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    algos: Mapped[list["Algo"]] = relationship(back_populates="server")


class Algo(Base):
    __tablename__ = "algos"
    __table_args__ = (UniqueConstraint("name", "server_id", name="uq_algo_name_server"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), nullable=False, index=True)
    script_path: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="STOPPED")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    server: Mapped["Server"] = relationship(back_populates="algos")


class AlgoRun(Base):
    __tablename__ = "algo_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    algo_id: Mapped[int] = mapped_column(ForeignKey("algos.id"), nullable=False, index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class Heartbeat(Base):
    __tablename__ = "heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    algo_id: Mapped[int] = mapped_column(ForeignKey("algos.id"), nullable=False, index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    cpu: Mapped[float | None] = mapped_column(Float, nullable=True)
    memory: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    position: Mapped[str | None] = mapped_column(String(120), nullable=True)


class Log(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    algo_id: Mapped[int | None] = mapped_column(ForeignKey("algos.id"), nullable=True, index=True)
    server_id: Mapped[int | None] = mapped_column(ForeignKey("servers.id"), nullable=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    event: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("algo_id", "server_id", "symbol", name="uq_position_algo_server_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    algo_id: Mapped[int] = mapped_column(ForeignKey("algos.id"), nullable=False, index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    average_price: Mapped[float] = mapped_column(Float, nullable=False)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    algo_id: Mapped[int] = mapped_column(ForeignKey("algos.id"), nullable=False, index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY / SELL
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DailyPnl(Base):
    __tablename__ = "daily_pnl"
    __table_args__ = (UniqueConstraint("algo_id", "server_id", "date", name="uq_daily_pnl_algo_server_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    algo_id: Mapped[int] = mapped_column(ForeignKey("algos.id"), nullable=False, index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), nullable=False, index=True)
    date: Mapped[datetime] = mapped_column(Date, nullable=False, index=True)
    pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class Command(Base):
    """Audit log for every control action, per the project's own security
    requirement that every START/STOP/UPDATE command be auditable.

    job_id maps to Milestone 4's Lambda response (the SSM CommandId) --
    added beyond the schema's minimal field list because it's the actual
    bridge between the async Lambda pattern already built and persistent
    command tracking here."""

    __tablename__ = "commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    algo_id: Mapped[int | None] = mapped_column(ForeignKey("algos.id"), nullable=True, index=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), nullable=False, index=True)
    command: Mapped[str] = mapped_column(String(20), nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    requested_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)


class RateLimitWindow(Base):
    """Milestone 12: fixed-window request counter per API key. DB-backed
    (not in-memory) because Vercel serverless functions share no memory
    between invocations -- an in-memory counter would silently reset on
    every cold start and never actually limit anything. Stores a hash of
    the key, never the key itself, even though there's currently only one
    shared key -- no reason to add a second place a real secret could leak
    from."""

    __tablename__ = "rate_limit_windows"
    __table_args__ = (UniqueConstraint("api_key_hash", "window_start", name="uq_rate_limit_key_window"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class User(Base):
    """Stage 18: a human dashboard operator.

    Auth is username + password (bcrypt hash, never the plaintext).
    Authorization is role-based: `role` maps to a fixed permission set in
    trading/api/security/permissions.py; `extra_permissions` is an
    optional per-user grant list layered on top (JSON array of permission
    names) for the rare case a user needs one capability beyond their
    role. A brand-new user defaults to role "viewer" (VIEW only) -- no
    one can control a trading process without an admin explicitly
    assigning a control role.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    extra_permissions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Forces a password change on next login (e.g. after an admin reset).
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class AuthSession(Base):
    """A refresh-token session. The opaque refresh token is delivered to
    the browser only as an httpOnly, Secure, SameSite=Strict cookie; only
    its SHA-256 hash is stored here, so a DB leak cannot be replayed. Each
    refresh rotates the token (new row, old row revoked). Logout / admin
    revoke sets revoked_at.
    """

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # Non-secret double-submit CSRF value bound to this session.
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_to: Mapped[int | None] = mapped_column(ForeignKey("auth_sessions.id"), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AuditLog(Base):
    """Append-only record of every security-relevant action: auth events,
    trading-process control (START/STOP/RESTART/UPDATE), EC2 power,
    server/algo registration, user administration, and every permission
    denial on a control route. Read by admins via GET /api/admin/audit.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    # "user:<id>" | "service:<name>" | "anonymous"
    actor: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    action: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="success")  # success|denied|error
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class StrategyHeartbeat(Base):
    """Dormant heartbeat table from the pre-consolidation monitoring API.

    One latest-state row per (strategy_name, server_name) pair. The
    endpoints that populated and read it (POST /update_strategy,
    GET /strategies) have been removed; the table, its columns, and its
    unique constraint are kept verbatim so its existing production rows
    are preserved. Current code uses the control-center Heartbeat / Algo
    tables above.
    """

    __tablename__ = "strategy_heartbeats"
    __table_args__ = (
        UniqueConstraint("strategy_name", "server_name", name="uq_strategy_server"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    strategy_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    server_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    current_mtm: Mapped[float] = mapped_column(Float, nullable=False)
    day_pnl: Mapped[float] = mapped_column(Float, nullable=False)
    number_of_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_update_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
    )
