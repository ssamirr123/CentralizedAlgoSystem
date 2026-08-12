"""
FastAPI dependencies for the control-center API: auth + DB session.

Auth here is a shared API key (X-API-Key header), not per-user identity --
a pragmatic minimum for this milestone. Real per-user auth (Supabase Auth,
JWT, role-based access) is a separate, larger concern deferred to
Milestone 12 (security hardening), matching this project's own roadmap.
"""
from __future__ import annotations

import os

from fastapi import Header, HTTPException, status
from sqlalchemy.orm import Session

from trading.database.connection import SessionLocal


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.environ.get("CONTROL_API_KEY")
    if not expected:
        # Fail closed: an unset key means auth cannot be enforced, so
        # refuse everything rather than silently allowing all requests
        # through because a deploy forgot to set the env var.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CONTROL_API_KEY is not configured on the server",
        )
    if not x_api_key or x_api_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
