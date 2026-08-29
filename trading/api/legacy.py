"""
Legacy monitoring API -- the original heartbeat endpoints that predate the
control-center /api/* surface.

    POST /update_strategy   upsert one latest-state row per (strategy, server)
    GET  /strategies        list all latest-state rows
    GET  /health            liveness probe

These are kept for backward compatibility: the existing Streamlit
dashboard polls GET /strategies, and strategy processes (example_strategy,
strategy_agent) still POST /update_strategy. Behavior here is a verbatim
move from backend/main.py -- same request/response shapes, status codes,
database writes, and Telegram alert calls. Nothing new, nothing changed.

The canonical API is the control-center router mounted at /api/*. New
functionality goes there, not here. This module will be removed once the
dashboard and strategy processes are migrated off it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from alerts.telegram import alert_service
from trading.core.config import load_settings
from trading.database.connection import get_db
from trading.database.models import StrategyHeartbeat

DAY_LOSS_LIMIT = load_settings().day_loss_limit

logger = logging.getLogger("strategy_monitor")


# --------------------------------------------------------------------------
# Schemas (moved verbatim from backend/schemas.py -- backend.schemas is now
# a re-export shim pointing here).
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------
router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        timestamp_utc=datetime.now(timezone.utc),
        service="central-strategy-monitor",
    )


@router.post(
    "/update_strategy",
    response_model=StrategyHeartbeatOut,
    status_code=status.HTTP_200_OK,
)
def update_strategy(
    payload: StrategyHeartbeatIn,
    db: Session = Depends(get_db),
) -> StrategyHeartbeatOut:
    """Create or update the latest heartbeat per strategy and server pair."""
    try:
        strategy = (
            db.query(StrategyHeartbeat)
            .filter(
                StrategyHeartbeat.strategy_name == payload.strategy_name,
                StrategyHeartbeat.server_name == payload.server_name,
            )
            .one_or_none()
        )

        new_status = payload.status.value
        s_name = payload.strategy_name
        srv_name = payload.server_name

        if strategy is None:
            strategy = StrategyHeartbeat(
                strategy_name=s_name,
                server_name=srv_name,
                status=new_status,
                current_mtm=payload.current_mtm,
                day_pnl=payload.day_pnl,
                number_of_trades=payload.number_of_trades,
                last_update_time=payload.last_update_time,
                received_at=datetime.now(timezone.utc),
            )
            db.add(strategy)
            # First-ever heartbeat for this strategy
            if new_status == StrategyStatus.RUNNING:
                alert_service.strategy_started(s_name, srv_name)
            elif new_status == StrategyStatus.STOPPED:
                alert_service.strategy_stopped(s_name, srv_name)
            elif new_status == StrategyStatus.ERROR:
                alert_service.strategy_crashed(s_name, srv_name, reason="Initial status ERROR")
        else:
            prev_status = strategy.status
            strategy.status = new_status
            strategy.current_mtm = payload.current_mtm
            strategy.day_pnl = payload.day_pnl
            strategy.number_of_trades = payload.number_of_trades
            strategy.last_update_time = payload.last_update_time
            strategy.received_at = datetime.now(timezone.utc)

            # Status transition alerts
            if prev_status != new_status:
                if new_status == StrategyStatus.RUNNING:
                    # Recovered from STOPPED or ERROR
                    alert_service.strategy_recovered(s_name, srv_name)
                elif new_status == StrategyStatus.STOPPED:
                    alert_service.strategy_stopped(s_name, srv_name)
                elif new_status == StrategyStatus.ERROR:
                    alert_service.strategy_crashed(s_name, srv_name, reason="Status changed to ERROR")

        # Day loss alert (fires on every heartbeat when over limit, dedup prevents spam)
        if payload.day_pnl < 0 and abs(payload.day_pnl) > DAY_LOSS_LIMIT:
            alert_service.day_loss_exceeded(s_name, srv_name, loss=payload.day_pnl, limit=DAY_LOSS_LIMIT)

        db.commit()
        db.refresh(strategy)
        return strategy
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Failed to process heartbeat")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while updating strategy heartbeat",
        ) from exc


@router.get("/strategies", response_model=list[StrategyHeartbeatOut])
def get_strategies(db: Session = Depends(get_db)) -> list[StrategyHeartbeatOut]:
    """List the latest known state for all strategies."""
    return (
        db.query(StrategyHeartbeat)
        .order_by(StrategyHeartbeat.received_at.desc())
        .all()
    )
