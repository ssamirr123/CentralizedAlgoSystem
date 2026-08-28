"""
FastAPI application factory.

create_app() is the single place the application object is assembled:
lifespan (DB init + the stale-heartbeat background watcher), the
control-center router at /api, and the legacy router
(/update_strategy, /strategies, /health).

backend/main.py stays import-compatible: `app = create_app()`.

Nothing about API behavior changes here -- the legacy endpoints are a
verbatim move into trading/api/legacy.py, and the control-center surface
is untouched.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from sqlalchemy.orm import Session

from alerts.telegram import alert_service
from trading.api.legacy import router as legacy_router
from trading.api.routes import router as control_center_router
from trading.database.connection import SessionLocal, init_db
from trading.database.models import StrategyHeartbeat

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
    # Single canonical Base since Stage 2 -- one init_db() creates every
    # table (control-center schema + strategy_heartbeats). Idempotent.
    init_db()
    logger.info("Database initialized at startup (canonical schema)")
    # Background watcher is disabled on serverless platforms (e.g. Vercel).
    # Set DISABLE_BACKGROUND_WATCHER=true in that environment.
    watcher_task = None
    if not os.environ.get("DISABLE_BACKGROUND_WATCHER", "").lower() in ("1", "true", "yes"):
        watcher_task = asyncio.create_task(_stale_heartbeat_watcher())
    else:
        logger.info("Background watcher disabled (serverless mode)")
    yield
    if watcher_task is not None:
        watcher_task.cancel()


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Central Strategy Monitoring API",
        version="1.0.0",
        description="Receives strategy heartbeats from distributed EC2 strategy workers.",
        lifespan=lifespan,
    )
    app.include_router(control_center_router, prefix="/api")
    app.include_router(legacy_router)
    return app
