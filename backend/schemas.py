from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class StrategyStatus(str, Enum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class StrategyHeartbeatIn(BaseModel):
    strategy_name: str = Field(..., min_length=1, max_length=120)
    server_name: str = Field(..., min_length=1, max_length=120)
    status: StrategyStatus
    current_mtm: float
    day_pnl: float
    number_of_trades: int = Field(..., ge=0)
    last_update_time: datetime


class StrategyHeartbeatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    strategy_name: str
    server_name: str
    status: StrategyStatus
    current_mtm: float
    day_pnl: float
    number_of_trades: int
    last_update_time: datetime
    received_at: datetime


class HealthResponse(BaseModel):
    status: str
    timestamp_utc: datetime
    service: str

