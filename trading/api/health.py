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
      "database": "connected"            # "connected" | "error: <ExceptionClass>"
    }

If the database is unreachable the endpoint still returns this exact
structure (status "degraded", database "error: ...") with a 503 status
code -- it never raises / 500s. The error string is the exception class
name only, never the connection string or any credential.

Phase 11 (Section 31) adds two purely-informational component blocks,
``ai_subsystem`` and ``market_data_subsystem``. Neither performs a
network call or contacts an external provider -- readiness (the 200/503
decision) is still driven by the database check ONLY, exactly as before;
per Section 31's own instruction, overall readiness must not depend on
every LLM provider being reachable. These blocks report configured
STATE (feature flags, feed status), not liveness of an external service.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Response
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from trading.ai_research.config import load_ai_research_settings
from trading.core.config import load_settings
from trading.database.connection import engine

SERVICE_NAME = "centralized-algo-backend"


class ControlCenterHealth(BaseModel):
    status: str
    service: str
    timestamp: datetime
    database: str
    ai_subsystem: dict
    market_data_subsystem: dict


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


def _ai_subsystem_status() -> dict:
    """Cheap, local, no network call -- just the resolved feature-flag
    state (Section 32: safe to report, never a secret)."""
    try:
        s = load_ai_research_settings()
        return {
            "research_enabled": s.enabled,
            "workloads_enabled": s.ai_workloads_enabled,
            "backtest_enabled": s.ai_backtest_enabled,
            "options_research_enabled": s.ai_options_research_enabled,
            "llm_provider": s.llm_provider,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": exc.__class__.__name__}


def _market_data_subsystem_status() -> dict:
    """Reuses the existing Stage 19 feed-status singleton -- also no
    network call; this is in-process state already maintained by the
    running feed worker (or its absence)."""
    try:
        core_settings = load_settings()
        from trading.market_data.status import FEED_STATUS

        snap = FEED_STATUS.snapshot()
        return {
            "market_data_enabled": core_settings.market_data_enabled,
            "options_intelligence_enabled": core_settings.options_intelligence_enabled,
            "feed_state": snap.get("feed_state"),
            "session_state": snap.get("session_state"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": exc.__class__.__name__}


@router.get("/health", response_model=ControlCenterHealth)
def health(response: Response) -> ControlCenterHealth:
    db_ok, db_detail = _check_database()
    if not db_ok:
        response.status_code = http_status.HTTP_503_SERVICE_UNAVAILABLE
    return ControlCenterHealth(
        status="ok" if db_ok else "degraded",
        service=SERVICE_NAME,
        timestamp=datetime.now(timezone.utc),
        database=db_detail,
        ai_subsystem=_ai_subsystem_status(),
        market_data_subsystem=_market_data_subsystem_status(),
    )
