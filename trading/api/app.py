"""
FastAPI application factory.

create_app() is the single place the application object is assembled:
lifespan (DB init + the stale-heartbeat background watcher), the
control-center router at /api, /api/health, and the legacy router
(/update_strategy, /strategies, /health).

backend/main.py stays import-compatible: `app = create_app()`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from trading.api.health import router as health_router
from trading.api.legacy import router as legacy_router
from trading.api.routes import router as control_center_router
from trading.api.watcher import stale_heartbeat_watcher
from trading.database.connection import init_db

logger = logging.getLogger("strategy_monitor")
logging.basicConfig(level=logging.INFO)


def _watcher_disabled() -> bool:
    return os.environ.get("DISABLE_BACKGROUND_WATCHER", "").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Single canonical Base since Stage 2 -- one init_db() creates every
    # table (control-center schema + strategy_heartbeats). Idempotent.
    init_db()
    logger.info("Database initialized at startup (canonical schema)")
    # Background watcher is disabled on serverless platforms (e.g. Vercel).
    # Set DISABLE_BACKGROUND_WATCHER=true in that environment.
    watcher_task = None
    if not _watcher_disabled():
        watcher_task = asyncio.create_task(stale_heartbeat_watcher())
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
    app.include_router(health_router, prefix="/api")  # GET /api/health, unauthenticated
    app.include_router(legacy_router)
    return app
