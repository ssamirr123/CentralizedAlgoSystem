"""
FastAPI application factory.

create_app() assembles: lifespan (DB init + stale-heartbeat watcher +
admin bootstrap), CORS, security headers, and the routers -- control-
center at /api, auth at /api/auth, admin at /api/admin, /api/health, and
the realtime WebSocket at /api/ws.

This is the single application entrypoint. Uvicorn runs it via the
factory: `uvicorn trading.api.app:create_app --factory`.
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
from trading.api.execution_routes import router as execution_router  # noqa: E402
from trading.api.execution_state import build_execution_state  # noqa: E402
from trading.api.health import router as health_router  # noqa: E402
from trading.api.market_routes import router as market_router  # noqa: E402
from trading.api.realtime.ws import router as realtime_router  # noqa: E402
from trading.api.routes import router as control_center_router  # noqa: E402
from trading.api.straddle_pulse_routes import router as straddle_pulse_router  # noqa: E402
from trading.api.security.bootstrap import bootstrap_admin  # noqa: E402
from trading.api.watcher import stale_heartbeat_watcher  # noqa: E402
from trading.api.worker_routes import router as worker_router  # noqa: E402
from trading.api.live_authorization_routes import router as live_authorization_router  # noqa: E402
from trading.core.config import load_settings  # noqa: E402
from trading.database.connection import init_db  # noqa: E402

logger = logging.getLogger("strategy_monitor")


def _watcher_disabled() -> bool:
    return load_settings().disable_background_watcher


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 15D-DR (Area A): the one required startup log line -- version,
    # Git SHA, deployment ID, environment, timestamp. Explicitly does NOT
    # authorize trading; see trading/common/deployment_info.py.
    from trading.common.deployment_info import get_deployment_info, log_startup_banner
    from trading.common.production_guard import check_production_safety_paths

    log_startup_banner(logger)

    # Phase 15D.8: fail closed, loudly, at startup -- before anything else
    # runs -- if this is a production-like environment missing durable
    # kill-switch/audit persistence. See trading/common/production_guard.py
    # for exactly what this checks; a no-op in every dev/test/CI run.
    check_production_safety_paths()

    # Phase 15D-AUDIT (Area L): the SAME identity, durably recorded to the
    # audit trail app.state.execution already carries (built synchronously
    # in create_app() below, before this lifespan runs) -- a log line alone
    # does not survive a log-rotation/retention policy, but this does when
    # AUDIT_DB_PATH is configured (see trading/api/execution_state.py). A
    # plain in-memory AuditTrail still accepts this call identically; it
    # simply won't survive restart, exactly like every other event on it.
    info = get_deployment_info()
    try:
        app.state.execution.audit_trail.append(
            "DEPLOYMENT_STARTUP", correlation_id="", strategy_id="",
            app_version=info.app_version, git_sha=info.git_sha,
            deployment_id=info.deployment_id, environment=info.environment,
            startup_timestamp=info.startup_timestamp,
        )
    except Exception:  # noqa: BLE001 -- an audit-write failure must never block startup
        logger.exception("failed to record DEPLOYMENT_STARTUP audit event")

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

    # Stage 19 market-data engine: opt-in. Probe the Breeze session, then
    # start the timezone-aware scheduler which drives the feed service's
    # 09:10 / 15:45 IST startup / stop flows. Never blocks API startup.
    md_settings = load_settings()
    md_scheduler = None
    if md_settings.market_data_enabled:
        try:
            from trading.market_data.scheduler import MarketDataScheduler
            from trading.market_data.service import get_service
            from trading.market_data.session import get_session_manager

            result = get_session_manager().check()
            logger.info("market_data.start session_check=%s", result.state.value)

            svc = get_service()
            md_scheduler = MarketDataScheduler(on_start=svc.on_start, on_stop=svc.on_stop)
            md_scheduler.start()
        except Exception:  # noqa: BLE001
            logger.exception("market-data engine failed to start")

    yield
    try:
        app.state.execution.audit_trail.append(
            "DEPLOYMENT_SHUTDOWN", correlation_id="", strategy_id="",
            app_version=info.app_version, git_sha=info.git_sha,
            deployment_id=info.deployment_id, environment=info.environment,
            shutdown_reason="normal shutdown",
        )
    except Exception:  # noqa: BLE001 -- an audit-write failure must never block shutdown
        logger.exception("failed to record DEPLOYMENT_SHUTDOWN audit event")
    if watcher_task is not None:
        watcher_task.cancel()
    if md_scheduler is not None:
        try:
            await md_scheduler.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("market-data scheduler shutdown error")


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

    # Phase 11: one ExecutionState per app instance -- see
    # trading/api/execution_state.py's module docstring for why this is
    # built fresh here (test isolation) rather than as a module-level
    # global. Never connects to a broker or touches a credential.
    app.state.execution = build_execution_state()

    app.include_router(auth_router, prefix="/api")  # /api/auth/*
    app.include_router(admin_router, prefix="/api")  # /api/admin/*
    app.include_router(control_center_router, prefix="/api")
    app.include_router(execution_router, prefix="/api")  # Phase 11: /api/strategies|accounts|brokers|assignments|risk|execution|system/*
    app.include_router(market_router, prefix="/api")  # /api/market/* (Stage 19 market data)
    app.include_router(straddle_pulse_router, prefix="/api")  # /api/market/straddle-pulse/*
    app.include_router(health_router, prefix="/api")  # GET /api/health, unauthenticated
    app.include_router(worker_router, prefix="/api")  # Phase 16.12: /api/worker/* (machine auth, not human RBAC)
    app.include_router(live_authorization_router, prefix="/api")  # Phase 17.1-R: /api/live-authorization/* (authenticated human operator only)
    if load_settings().realtime_enabled:
        app.include_router(realtime_router, prefix="/api")  # WS /api/ws (Stage 19)
    return app
