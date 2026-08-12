from __future__ import annotations

import os
import asyncio
from datetime import datetime, timedelta, timezone
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.database import SessionLocal, get_db, init_db
from backend.models import StrategyHeartbeat
from backend.schemas import HealthResponse, StrategyHeartbeatIn, StrategyHeartbeatOut, StrategyStatus
from alerts.telegram import alert_service
from trading.api.routes import router as control_center_router
from trading.database.connection import init_db as init_control_center_db

DAY_LOSS_LIMIT = float(os.environ.get("DAY_LOSS_LIMIT", "10000.0"))
STALE_THRESHOLD_MINUTES = float(os.environ.get("STALE_THRESHOLD_MINUTES", "2"))
STALE_CHECK_INTERVAL_SECONDS = int(os.environ.get("STALE_CHECK_INTERVAL_SECONDS", "60"))

logger = logging.getLogger("strategy_monitor")
logging.basicConfig(level=logging.INFO)


async def _stale_heartbeat_watcher() -> None:
    """Background task: fires heartbeat_missing alert for strategies silent > threshold."""
    logger.info("Stale heartbeat watcher started (interval=%ds)", STALE_CHECK_INTERVAL_SECONDS)
    while True:
        await asyncio.sleep(STALE_CHECK_INTERVAL_SECONDS)
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_THRESHOLD_MINUTES)
            db: Session = SessionLocal()
            try:
                stale = (
                    db.query(StrategyHeartbeat)
                    .filter(StrategyHeartbeat.received_at < cutoff)
                    .all()
                )
                for s in stale:
                    silent_for = (
                        datetime.now(timezone.utc) - s.received_at.replace(tzinfo=timezone.utc)
                    ).total_seconds() / 60
                    logger.warning(
                        "Stale heartbeat detected | strategy=%s | server=%s | silent=%.1f min",
                        s.strategy_name, s.server_name, silent_for,
                    )
                    alert_service.heartbeat_missing(
                        s.strategy_name, s.server_name, minutes=silent_for
                    )
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error in stale heartbeat watcher: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    init_control_center_db()
    logger.info("Database initialized at startup (heartbeat monitor + control center schemas)")
    # Background watcher is disabled on serverless platforms (e.g. Vercel)
    # Set DISABLE_BACKGROUND_WATCHER=true in Vercel environment variables
    watcher_task = None
    if not os.environ.get("DISABLE_BACKGROUND_WATCHER", "").lower() in ("1", "true", "yes"):
        watcher_task = asyncio.create_task(_stale_heartbeat_watcher())
    else:
        logger.info("Background watcher disabled (serverless mode)")
    yield
    if watcher_task is not None:
        watcher_task.cancel()

app = FastAPI(
    title="Central Strategy Monitoring API",
    version="1.0.0",
    description="Receives strategy heartbeats from distributed EC2 strategy workers.",
    lifespan=lifespan,
)
app.include_router(control_center_router, prefix="/api")


@app.post(
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


@app.get("/strategies", response_model=list[StrategyHeartbeatOut])
def get_strategies(db: Session = Depends(get_db)) -> list[StrategyHeartbeatOut]:
    """List the latest known state for all strategies."""
    return (
        db.query(StrategyHeartbeat)
        .order_by(StrategyHeartbeat.received_at.desc())
        .all()
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        timestamp_utc=datetime.now(timezone.utc),
        service="central-strategy-monitor",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=False)

