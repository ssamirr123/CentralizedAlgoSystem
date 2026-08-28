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
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Response
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from trading.database.connection import engine

SERVICE_NAME = "centralized-algo-backend"


class ControlCenterHealth(BaseModel):
    status: str
    service: str
    timestamp: datetime
    database: str


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
    return ControlCenterHealth(
        status="ok" if db_ok else "degraded",
        service=SERVICE_NAME,
        timestamp=datetime.now(timezone.utc),
        database=db_detail,
    )
