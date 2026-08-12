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
    status: str
    last_heartbeat: datetime | None


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
