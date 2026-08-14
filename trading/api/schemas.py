from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


class AlgoActionRequest(BaseModel):
    algo_id: str  # algo NAME (e.g. "example_strategy") -- matches Lambda/agent convention
    server_id: str  # server NAME (servers.name), not the integer PK
    requested_by: str | None = None


class LogsQuery(BaseModel):
    algo_id: str
    limit: int = 100


class CommandResponse(BaseModel):
    success: bool
    command_id: int | None = None
    job_id: str | None = None
    status: str
    message: str | None = None


class ServerStatusResponse(BaseModel):
    name: str
    ec2_instance_id: str
    region: str
    status: str  # DB-cached value; only refreshed on live=true
    last_heartbeat: datetime | None
    # Populated only when ?live=true triggers a real check_ec2_health call
    # (Milestone 12) -- None otherwise, not a stale/fabricated default.
    ssm_status: str | None = None
    live_check_healthy: bool | None = None


class AlgoStatusResponse(BaseModel):
    success: bool
    algo_id: str
    status: str
    pid: int | None = None
    started_at: str | None = None
    message: str | None = None


class LogEntry(BaseModel):
    timestamp: datetime
    level: str
    event: str
    details: dict[str, Any] | None = None


class LogIn(BaseModel):
    algo_id: str  # algo NAME
    server_id: str  # server NAME
    level: str
    event: str
    details: dict[str, Any] | None = None
    timestamp: datetime | None = None  # server sets to now if omitted


class LogAck(BaseModel):
    success: bool


class PositionEntry(BaseModel):
    symbol: str
    quantity: int
    average_price: float
    last_price: float | None
    pnl: float | None
    updated_at: datetime


class DailyPnlEntry(BaseModel):
    date: date
    pnl: float
    trade_count: int


class AlgoListEntry(BaseModel):
    algo_id: str  # algo name
    server_id: str  # server name
    status: str
    enabled: bool
    script_path: str
    updated_at: datetime
    last_heartbeat: datetime | None = None


class AlgoIn(BaseModel):
    algo_id: str  # algo NAME -- unique per server, not globally
    server_id: str  # server NAME (servers.name) -- must already be registered
    script_path: str | None = None  # defaults to trading/algos/{algo_id}/main.py if omitted
    status: str = "STOPPED"
    enabled: bool = True


class AlgoUpdate(BaseModel):
    # All optional -- PATCH semantics, only provided fields are changed.
    # Renaming isn't supported here (unlike servers) -- algo identity is
    # (name, server_id), and start/stop/heartbeat all address an algo by
    # that pair, so a rename would silently orphan every future call
    # still using the old name.
    script_path: str | None = None
    status: str | None = None
    enabled: bool | None = None


class AlgoRegisterResponse(BaseModel):
    algo: AlgoListEntry
    # Best-effort code sync to the (currently single, hardcoded) target
    # EC2 instance, attempted right after registration -- sync_attempted
    # is False only if the Lambda itself couldn't be reached at all; a
    # git pull that ran but failed (merge conflict, network blip) still
    # sets sync_attempted=True, sync_success=False. Registration always
    # succeeds regardless of sync outcome -- the DB row is the source of
    # truth, code deployment is a separate, retriable concern.
    sync_attempted: bool
    sync_success: bool | None = None
    sync_message: str | None = None


class HeartbeatIn(BaseModel):
    algo_id: str  # algo NAME
    server_id: str  # server NAME
    status: str
    cpu: float | None = None
    memory: float | None = None
    pnl: float | None = None
    position: str | None = None
    timestamp: datetime | None = None  # server sets to now if omitted


class HeartbeatAck(BaseModel):
    success: bool
    algo_id: str
    server_id: str


class ServerListEntry(BaseModel):
    server_id: str  # server name
    ec2_instance_id: str
    region: str
    status: str
    last_heartbeat: datetime | None


class ServerIn(BaseModel):
    server_id: str  # server NAME (servers.name) -- must be unique
    ec2_instance_id: str
    region: str
    status: str = "UNKNOWN"


class ServerUpdate(BaseModel):
    # All optional -- PATCH semantics, only provided fields are changed.
    server_id: str | None = None  # rename (servers.name); new value must stay unique
    ec2_instance_id: str | None = None
    region: str | None = None
    status: str | None = None


class PositionIn(BaseModel):
    algo_id: str  # algo NAME
    server_id: str  # server NAME
    symbol: str
    quantity: int
    average_price: float
    last_price: float | None = None
    pnl: float | None = None


class PositionAck(BaseModel):
    success: bool
    closed: bool = False  # True if quantity==0 caused the position row to be deleted


class TradeIn(BaseModel):
    algo_id: str  # algo NAME
    server_id: str  # server NAME
    symbol: str
    side: str  # BUY / SELL
    quantity: int
    price: float
    order_id: str | None = None
    executed_at: datetime | None = None  # server sets to now if omitted


class TradeAck(BaseModel):
    success: bool


class TradeEntry(BaseModel):
    symbol: str
    side: str
    quantity: int
    price: float
    executed_at: datetime
    order_id: str | None = None


class DailyPnlIn(BaseModel):
    algo_id: str  # algo NAME
    server_id: str  # server NAME
    pnl: float
    trade_count: int = 0
    # Named pnl_date, NOT date -- a field named the same as its own `date`
    # type annotation self-shadows during Pydantic's postponed-annotation
    # evaluation (`date | None` fails with "unsupported operand type(s) for
    # |: 'NoneType' and 'NoneType'" because by eval time the class already
    # bound `date` to its own default value, None). Same reasoning as
    # LogIn's log_date field.
    pnl_date: date | None = None  # server sets to today (UTC) if omitted
