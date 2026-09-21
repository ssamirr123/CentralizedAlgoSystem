"""
Control-center health endpoint: GET /api/health.

Unauthenticated liveness/readiness probe for the canonical API. Separate
from the legacy GET /health (trading/api/legacy.py), which is left
untouched. This one additionally reports database reachability via a
lightweight `SELECT 1`.

    {
      "status": "ok",                    # "ok" | "degraded"
      "service": "centralized-algo-backend",
      "timestamp": "2026-08-28T12:00:00Z",
      "database": "connected",           # "connected" | "error: <ExceptionClass>"
      "app_version": "0.0.0-dev",
      "git_sha": "a1b2c3d",
      "deployment_id": "local-12345",
      "environment": "development"
    }

If the database is unreachable the endpoint still returns this exact
structure (status "degraded", database "error: ...") with a 503 status
code -- it never raises / 500s. The error string is the exception class
name only, never the connection string or any credential.

Phase 15D-DR (Area A): also exposes GET /api/ready, which is deliberately
a DIFFERENT, separate endpoint from /health -- an application can be
"healthy" (process up, DB reachable) while trading is nowhere near
authorized, and this project must never let a deployment/orchestrator
health check be mistaken for, or accidentally gate, that authorization.
/ready reports three explicitly distinct fields (application, broker,
trading_authorized) rather than a single boolean, specifically so nothing
can collapse "the app started" into "trading is live".
"""
from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from trading.common.deployment_info import get_deployment_info
from trading.common.time_sync import check_clock_drift, configured_warn_threshold_seconds
from trading.database.connection import engine

SERVICE_NAME = "centralized-algo-backend"

# Phase 15D.8: the broker-readiness check below must never make /api/ready
# a potentially-blocking broker health check -- this bounds the ENTIRE
# check (including thread handoff) to a small, fixed wall-clock budget.
_BROKER_READINESS_TIMEOUT_SECONDS = 1.5


class ControlCenterHealth(BaseModel):
    status: str
    service: str
    timestamp: datetime
    database: str
    app_version: str
    git_sha: str
    deployment_id: str
    environment: str
    # Phase 15D.8: "not_checked" unless TIME_SYNC_* is wired to a real
    # reference clock (none is, by default -- see trading/common/time_sync.py).
    # Purely informational: NEVER affects `status`/the 503 decision above,
    # and never changes trading behavior -- a human reads this, nothing
    # automated acts on it.
    clock_drift: str


class ControlCenterReadiness(BaseModel):
    application: str  # "ready" | "not_ready"
    broker: str  # "connected" | "not_connected" | "unknown"
    trading_authorized: bool  # ALWAYS false unless a real, explicit human authorization exists elsewhere
    timestamp: datetime


router = APIRouter()


def _check_database() -> tuple[bool, str]:
    """Return (ok, detail). Never raises."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "connected"
    except SQLAlchemyError as exc:
        return False, f"error: {exc.__class__.__name__}"
    except Exception as exc:  # noqa: BLE001  -- health must not crash on anything
        return False, f"error: {exc.__class__.__name__}"


@router.get("/health", response_model=ControlCenterHealth)
def health(response: Response) -> ControlCenterHealth:
    db_ok, db_detail = _check_database()
    if not db_ok:
        response.status_code = http_status.HTTP_503_SERVICE_UNAVAILABLE
    info = get_deployment_info()
    drift = check_clock_drift(warn_threshold_seconds=configured_warn_threshold_seconds())
    return ControlCenterHealth(
        status="ok" if db_ok else "degraded",
        service=SERVICE_NAME,
        timestamp=datetime.now(timezone.utc),
        database=db_detail,
        app_version=info.app_version,
        git_sha=info.git_sha,
        deployment_id=info.deployment_id,
        environment=info.environment,
        clock_drift=drift.status,
    )


def _check_broker_readiness(broker_manager: object | None) -> str:
    """Phase 15D.8: reports the CURRENT connection state of whatever
    broker client(s) are already attached to a registered account --
    never instantiates one. This is the critical invariant preserved from
    trading/api/execution_state.py's own docstring ("no endpoint ...
    ever calls BrokerManager.get_broker() on it, so that factory is never
    actually invoked"): calling get_broker() here would lazily connect a
    broker (a real network/credential operation) just because someone
    polled a readiness endpoint, which is exactly the "potentially
    blocking broker health check" this function must not become.

    Bounded to _BROKER_READINESS_TIMEOUT_SECONDS total (thread handoff
    included) via a one-shot thread pool; read-only (`is_connected()`
    only -- never `connect()`, never any order/mutation call); fails
    safe on any exception or timeout by reporting "not_connected", never
    raising. Distinct from `application` (process/DB readiness, checked
    separately above) -- see module docstring."""
    if broker_manager is None:
        return "unknown"
    try:
        attached = [a.broker_client for a in broker_manager.accounts() if a.broker_client is not None]
    except Exception:  # noqa: BLE001 -- fail safe, never raise out of a readiness probe
        return "unknown"
    if not attached:
        return "not_configured"

    def _any_connected() -> bool:
        for client in attached:
            try:
                if client.is_connected():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    # Deliberately NOT a `with ThreadPoolExecutor(...) as pool:` block --
    # that context manager's __exit__ calls shutdown(wait=True), which
    # would block on exactly the slow/hung call this timeout exists to
    # bound. shutdown(wait=False) below lets a still-running probe thread
    # finish on its own time without this request waiting for it; it only
    # ever calls the read-only is_connected(), so an orphaned thread is
    # harmless.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_any_connected)
        connected = future.result(timeout=_BROKER_READINESS_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError:
        return "not_connected"
    except Exception:  # noqa: BLE001
        return "not_connected"
    finally:
        pool.shutdown(wait=False)
    return "connected" if connected else "not_connected"


@router.get("/ready", response_model=ControlCenterReadiness)
def ready(request: Request = None) -> ControlCenterReadiness:  # type: ignore[assignment]
    """Deliberately separate from /health (see module docstring).
    `application` reflects PROCESS readiness (DB reachable) only; `broker`
    reflects BROKER readiness only (see `_check_broker_readiness()`) --
    neither can block or slow down the other, and a broker outage never
    turns this endpoint into a 503 by itself (an orchestrator restarting
    on `application` alone must never accidentally key off a broker
    outage this app cannot fix by restarting). `trading_authorized` is
    hard-coded `False`: no code path exists (or should ever exist) that
    lets a readiness probe grant trading authorization -- that requires
    the explicit human gate documented in docs/phase-15d-2-live-canary-report.md,
    never a health check.

    `request` is optional (default None) so this function stays directly
    unit-testable without a full app/request context -- exactly how
    Phase 15D-DR's own tests already call it. FastAPI always supplies a
    real Request when this runs as the actual /api/ready route; a direct
    Python call with no request simply reports `broker="unknown"`,
    identical to this endpoint's pre-Phase-15D.8 behavior."""
    db_ok, _ = _check_database()
    app = getattr(request, "app", None) if request is not None else None
    state = getattr(app, "state", None)
    execution = getattr(state, "execution", None) if state is not None else None
    broker_manager = getattr(execution, "broker_manager", None) if execution is not None else None
    return ControlCenterReadiness(
        application="ready" if db_ok else "not_ready",
        broker=_check_broker_readiness(broker_manager),
        trading_authorized=False,
        timestamp=datetime.now(timezone.utc),
    )
