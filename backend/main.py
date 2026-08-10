"""
FastAPI backend — in-memory store (no database required).

All routes are mounted under /api by asgi_app.py.
EC2 workers POST to:  https://<your-streamlit-app>/api/update_strategy
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
import logging

from fastapi import FastAPI, status

from backend.schemas import HealthResponse, StrategyHeartbeatIn, StrategyHeartbeatOut, StrategyStatus
from alerts.telegram import alert_service

DAY_LOSS_LIMIT = float(os.environ.get("DAY_LOSS_LIMIT", "10000.0"))

logger = logging.getLogger("strategy_monitor")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# In-memory store  key: (strategy_name, server_name) → StrategyHeartbeatOut
# NOTE: resets on every redeploy — add a DB when persistence is needed
# ---------------------------------------------------------------------------
_store: dict[tuple[str, str], StrategyHeartbeatOut] = {}

app = FastAPI(
    title="Central Strategy Monitoring API",
    version="1.0.0",
    description="Receives strategy heartbeats from distributed EC2 strategy workers.",
)


@app.post(
    "/update_strategy",
    response_model=StrategyHeartbeatOut,
    status_code=status.HTTP_200_OK,
)
def update_strategy(payload: StrategyHeartbeatIn) -> StrategyHeartbeatOut:
    """Create or update the latest heartbeat per strategy and server pair."""
    key = (payload.strategy_name, payload.server_name)
    now = datetime.now(timezone.utc)
    new_status = payload.status.value
    s_name = payload.strategy_name
    srv_name = payload.server_name

    existing = _store.get(key)

    if existing is None:
        if new_status == StrategyStatus.RUNNING:
            alert_service.strategy_started(s_name, srv_name)
        elif new_status == StrategyStatus.STOPPED:
            alert_service.strategy_stopped(s_name, srv_name)
        elif new_status == StrategyStatus.ERROR:
            alert_service.strategy_crashed(s_name, srv_name, reason="Initial status ERROR")
    else:
        prev_status = existing.status.value
        if prev_status != new_status:
            if new_status == StrategyStatus.RUNNING:
                alert_service.strategy_recovered(s_name, srv_name)
            elif new_status == StrategyStatus.STOPPED:
                alert_service.strategy_stopped(s_name, srv_name)
            elif new_status == StrategyStatus.ERROR:
                alert_service.strategy_crashed(s_name, srv_name, reason="Status changed to ERROR")

    if payload.day_pnl < 0 and abs(payload.day_pnl) > DAY_LOSS_LIMIT:
        alert_service.day_loss_exceeded(s_name, srv_name, loss=payload.day_pnl, limit=DAY_LOSS_LIMIT)

    record = StrategyHeartbeatOut(
        strategy_name=s_name,
        server_name=srv_name,
        status=payload.status,
        current_mtm=payload.current_mtm,
        day_pnl=payload.day_pnl,
        number_of_trades=payload.number_of_trades,
        last_update_time=payload.last_update_time,
        received_at=now,
    )
    _store[key] = record
    logger.info("Heartbeat received | strategy=%s | server=%s | status=%s", s_name, srv_name, new_status)
    return record


@app.get("/strategies", response_model=list[StrategyHeartbeatOut])
def get_strategies() -> list[StrategyHeartbeatOut]:
    """List the latest known state for all strategies."""
    return sorted(_store.values(), key=lambda s: s.received_at, reverse=True)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        timestamp_utc=datetime.now(timezone.utc),
        service="central-strategy-monitor",
    )
