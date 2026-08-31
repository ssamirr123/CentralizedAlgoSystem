"""
FastAPI application factory.

create_app() assembles: lifespan (DB init + stale-heartbeat watcher +
Stage 18 admin bootstrap), CORS, security headers, and the routers --
control-center at /api, auth at /api/auth, admin at /api/admin,
/api/health, and the legacy router.

backend/main.py stays import-compatible: `app = create_app()`.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# Configure the root logger before importing the sub-modules below -- some
# of them (via alerts.telegram) log at import time, and we want those
# lines to go through the canonical JSON handler too.
from trading.common.logger import configure_logging

configure_logging()

from trading.api.admin_routes import router as admin_router  # noqa: E402
from trading.api.auth_routes import router as auth_router  # noqa: E402
from trading.api.health import router as health_router  # noqa: E402
from trading.api.legacy import router as legacy_router  # noqa: E402
from trading.api.routes import router as control_center_router  # noqa: E402
from trading.api.security.bootstrap import bootstrap_admin  # noqa: E402
from trading.api.watcher import stale_heartbeat_watcher  # noqa: E402
from trading.core.config import load_settings  # noqa: E402
from trading.database.connection import init_db  # noqa: E402

logger = logging.getLogger("strategy_monitor")


def _watcher_disabled() -> bool:
    return load_settings().disable_background_watcher


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Single canonical Base since Stage 2 -- one init_db() creates every
    # table (control-center schema + auth tables + strategy_heartbeats).
    init_db()
    logger.info("Database initialized at startup (canonical schema)")

    # Stage 18: if AUTH_BOOTSTRAP_ADMIN_* is set and no users exist yet,
    # create the first admin. No-op otherwise.
    try:
        bootstrap_admin()
    except Exception:  # noqa: BLE001 -- never block startup on this
        logger.exception("admin bootstrap failed")

    watcher_task = None
    if not _watcher_disabled():
        watcher_task = asyncio.create_task(stale_heartbeat_watcher())
    else:
        logger.info("Background watcher disabled (serverless mode)")
    yield
    if watcher_task is not None:
        watcher_task.cancel()


def _configure_cors(app: FastAPI) -> None:
    raw = load_settings().auth_allowed_origins
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if not origins:
        # No cross-origin callers configured -> don't emit CORS headers at
        # all. The dashboard is served same-origin through CloudFront in
        # production, so this is the safe default.
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-API-Key"],
        expose_headers=["Retry-After"],
        max_age=600,
    )
    logger.info("CORS enabled for origins: %s", origins)


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Central Strategy Monitoring API",
        version="1.0.0",
        description="Control-center API with per-user auth + RBAC (Stage 18).",
        lifespan=lifespan,
    )

    _configure_cors(app)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    app.include_router(auth_router, prefix="/api")  # /api/auth/*
    app.include_router(admin_router, prefix="/api")  # /api/admin/*
    app.include_router(control_center_router, prefix="/api")
    app.include_router(health_router, prefix="/api")  # GET /api/health, unauthenticated
    app.include_router(legacy_router)  # legacy machine/streamlit endpoints (unauthenticated, slated for removal)
    return app
