"""
FastAPI dependencies for the control-center API: auth + rate limiting + DB
session.

Auth here is a shared API key (X-API-Key header), not per-user identity --
a pragmatic minimum. Real per-user auth (Supabase Auth, JWT, role-based
access) stays a deliberately deferred, larger feature -- flagged since
Milestone 6, still true here in Milestone 12. Supabase Row-Level Security
doesn't fit either: RLS gates access for Supabase's client SDK using JWT-
based sessions, and this project connects via a direct Postgres
connection string (SQLAlchemy/psycopg2), never that SDK -- there's no
session for RLS policies to evaluate against.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from fastapi import Header, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading.database import models
from trading.database.connection import SessionLocal

RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("RATE_LIMIT_MAX_REQUESTS", "60"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))


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


def enforce_rate_limit(x_api_key: str | None = Header(default=None)) -> None:
    """DB-backed fixed-window limiter (RATE_LIMIT_MAX_REQUESTS per
    RATE_LIMIT_WINDOW_SECONDS, per API key). Runs AFTER require_api_key
    in the router's dependency list, so an invalid key 401s before this
    ever queries the DB.

    Fixed-window, not sliding-window or token-bucket: simplest correct
    option for a single shared key. The insert-if-missing path below is a
    read-then-write, not a single atomic UPSERT, so it races when two
    requests hit the same brand-new window at once -- this is NOT a
    theoretical concern: a single algo process alone runs the control-
    center heartbeat, log shipper, and P&L reporter concurrently, and hit
    this exact race (an unhandled 500 from a UNIQUE constraint violation)
    during Milestone 12's own integration testing. Handled below by
    catching the constraint violation and falling back to the update path
    -- correct without needing a dialect-specific ON CONFLICT DO UPDATE.
    """
    if not x_api_key:
        return  # require_api_key (runs first) already rejects this request

    key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    window_epoch = (int(now.timestamp()) // RATE_LIMIT_WINDOW_SECONDS) * RATE_LIMIT_WINDOW_SECONDS
    window_start = datetime.fromtimestamp(window_epoch, tz=timezone.utc)

    db = SessionLocal()
    try:
        row = (
            db.query(models.RateLimitWindow)
            .filter(
                models.RateLimitWindow.api_key_hash == key_hash,
                models.RateLimitWindow.window_start == window_start,
            )
            .one_or_none()
        )
        if row is None:
            try:
                db.add(models.RateLimitWindow(api_key_hash=key_hash, window_start=window_start, request_count=1))
                db.commit()
                return
            except IntegrityError:
                # Lost the race: another concurrent request inserted this
                # window's row between our SELECT and our INSERT. Roll
                # back and re-fetch -- it's guaranteed to exist now.
                db.rollback()
                row = (
                    db.query(models.RateLimitWindow)
                    .filter(
                        models.RateLimitWindow.api_key_hash == key_hash,
                        models.RateLimitWindow.window_start == window_start,
                    )
                    .one()
                )

        if row.request_count >= RATE_LIMIT_MAX_REQUESTS:
            retry_after = RATE_LIMIT_WINDOW_SECONDS - int(now.timestamp() - window_epoch)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {RATE_LIMIT_MAX_REQUESTS} requests per {RATE_LIMIT_WINDOW_SECONDS}s",
                headers={"Retry-After": str(max(retry_after, 1))},
            )

        row.request_count += 1
        db.commit()
    finally:
        db.close()


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
