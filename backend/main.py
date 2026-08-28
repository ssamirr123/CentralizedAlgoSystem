"""
Application entrypoint.

The FastAPI object is now assembled by trading.api.app.create_app()
(Stage 4). This module stays import-compatible -- `app = create_app()` --
so api/index.py (`from backend.main import app`), `uvicorn
backend.main:app`, and the existing tests keep working unchanged.

The legacy heartbeat endpoints (/update_strategy, /strategies) are still
defined here, attached to the app the factory returns. They are not
migrated yet; that is a later stage. Their behavior is unchanged.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from alerts.telegram import alert_service
from backend.schemas import StrategyHeartbeatIn, StrategyHeartbeatOut, StrategyStatus
from trading.api.app import create_app
from trading.database.connection import get_db
from trading.database.models import StrategyHeartbeat

DAY_LOSS_LIMIT = float(os.environ.get("DAY_LOSS_LIMIT", "10000.0"))

logger = logging.getLogger("strategy_monitor")

app = create_app()


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=False)
