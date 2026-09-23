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

from trading.ai_research.backtest.router import router as ai_research_backtest_router  # noqa: E402
from trading.ai_research.market.router import router as ai_research_market_router  # noqa: E402
from trading.ai_research.market_data.router import router as ai_research_market_data_router  # noqa: E402
from trading.ai_research.router import router as ai_research_router  # noqa: E402
from trading.api.ai_options_research_routes import router as ai_options_research_router  # noqa: E402
from trading.api.admin_routes import router as admin_router  # noqa: E402
from trading.api.auth_routes import router as auth_router  # noqa: E402
from trading.api.health import router as health_router  # noqa: E402
from trading.api.market_routes import router as market_router  # noqa: E402
from trading.api.options_routes import router as options_router  # noqa: E402
from trading.api.realtime.ws import router as realtime_router  # noqa: E402
from trading.api.routes import router as control_center_router  # noqa: E402
from trading.api.straddle_pulse_routes import router as straddle_pulse_router  # noqa: E402
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

    # Phase 5 (AI Research): any run still QUEUED/RUNNING at startup
    # belongs to a process that no longer exists -- mark it FAILED
    # (error_category="INTERRUPTED") rather than silently claim it
    # completed, or leave it stuck forever. Never blocks API startup.
    try:
        from trading.ai_research.service import recover_interrupted_jobs

        recover_interrupted_jobs()
    except Exception:  # noqa: BLE001
        logger.exception("ai_research interrupted-job recovery failed")

    # Phase 6 (AI Research Backtesting): same recovery discipline as above,
    # for backtest jobs -- a stale QUEUED/RUNNING backtest (and any of its
    # still-RUNNING cells) is marked FAILED honestly; already-COMPLETED
    # cells are untouched so a later /resume only repeats real work.
    try:
        from trading.ai_research.backtest.service import recover_interrupted_backtests

        recover_interrupted_backtests()
    except Exception:  # noqa: BLE001
        logger.exception("ai_research backtest interrupted-job recovery failed")

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

    app.include_router(auth_router, prefix="/api")  # /api/auth/*
    app.include_router(admin_router, prefix="/api")  # /api/admin/*
    app.include_router(control_center_router, prefix="/api")
    app.include_router(market_router, prefix="/api")  # /api/market/* (Stage 19 market data)
    app.include_router(options_router, prefix="/api")  # /api/options/* (Phase 9 options intelligence)
    app.include_router(straddle_pulse_router, prefix="/api")  # /api/market/straddle-pulse/*
    app.include_router(health_router, prefix="/api")  # GET /api/health, unauthenticated
    # /api/ai-research/* -- always registered (route existence has no cost;
    # every handler checks AI_RESEARCH_ENABLED first and returns
    # ResearchDisabledOut, not an error, when it's off -- the default).
    # tradingagents is never imported here or at any module level in this
    # package, only lazily inside run_research() when actually enabled.
    # IMPORTANT: backtests/market routers MUST be included BEFORE
    # ai_research_router -- that router's GET /ai-research/{research_id}
    # is a catch-all path param that would otherwise match
    # "/ai-research/backtests" or "/ai-research/markets" first (FastAPI
    # matches across included routers in registration order).
    app.include_router(ai_research_backtest_router, prefix="/api")
    app.include_router(ai_research_market_router, prefix="/api")
    app.include_router(ai_research_market_data_router, prefix="/api")
    app.include_router(ai_research_router, prefix="/api")
    app.include_router(ai_options_research_router, prefix="/api")
    if load_settings().realtime_enabled:
        app.include_router(realtime_router, prefix="/api")  # WS /api/ws (Stage 19)
    return app
