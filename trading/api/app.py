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
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Configure the root logger before importing the sub-modules below -- some
# of them (via alerts.telegram) log at import time, and we want those
# lines to go through the canonical JSON handler too.
from trading.common.logger import configure_logging

configure_logging()

from trading.api.health import router as health_router  # noqa: E402
from trading.api.legacy import router as legacy_router  # noqa: E402
from trading.api.routes import router as control_center_router  # noqa: E402
from trading.api.watcher import stale_heartbeat_watcher  # noqa: E402
from trading.core.config import load_settings  # noqa: E402
from trading.database.connection import init_db  # noqa: E402

logger = logging.getLogger("strategy_monitor")


def _watcher_disabled() -> bool:
    return load_settings().disable_background_watcher


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
