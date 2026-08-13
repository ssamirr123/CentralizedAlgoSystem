"""
Trading control center schema. Minimum tables per the project's own
design: servers, algos, algo_runs, heartbeats, logs, positions, trades,
daily_pnl, commands.

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
