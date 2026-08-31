"""
FastAPI dependencies for the control-center API: authentication,
authorization (RBAC), rate limiting, DB session.

Stage 18 replaced the single shared X-API-Key with two authentication
lanes:

* Humans -> username/password -> short-lived JWT access token in the
  Authorization: Bearer header (refresh handled by an httpOnly cookie,
  see trading/api/auth_routes.py). Authorization is role-based; see
  trading/api/security/permissions.py.
* Machines (strategy processes posting telemetry) -> the X-API-Key
  header, which now maps to ONE fixed service identity with only VIEW +
  ingest rights. It can never start/stop/restart a process or reach
  administration, regardless of how it leaks.

`get_principal` resolves whichever lane the caller used and attaches the
result to request.state.principal. Route handlers add
`Depends(require_permission(Permission.X))` for the capability they need.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading.api.security import audit
from trading.api.security.permissions import (
    SERVICE_PERMISSIONS,
    Permission,
    permissions_for,
)
from trading.api.security.tokens import TokenError, decode_access_token
from trading.core.config import load_settings
from trading.database import models
from trading.database.connection import SessionLocal

logger = logging.getLogger("trading.api")

_settings = load_settings()
RATE_LIMIT_MAX_REQUESTS = _settings.rate_limit_max_requests
RATE_LIMIT_WINDOW_SECONDS = _settings.rate_limit_window_seconds

SERVICE_ACTOR = "service:control-api-key"
SERVICE_LABEL = "control-api-key (machine)"


# --------------------------------------------------------------------------
# Principal
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Principal:
    kind: str  # "user" | "service"
    permissions: frozenset[Permission]
    user_id: int | None = None
    username: str | None = None
    role: str | None = None
    label: str = ""
    must_change_password: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def actor(self) -> str:
        return f"user:{self.user_id}" if self.kind == "user" else SERVICE_ACTOR

    def has(self, perm: Permission) -> bool:
        return perm in self.permissions


def client_ip(request: Request) -> str:
    # nginx on the backend sets X-Forwarded-For; CloudFront prepends the
    # viewer IP. Take the first hop.
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------
# DB session
# --------------------------------------------------------------------------
def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
def _service_principal_from_key(presented: str | None) -> Principal | None:
    expected = load_settings().control_api_key
    if not expected or not presented:
        return None
    if not hmac.compare_digest(presented, expected):
        return None
    return Principal(
        kind="service",
        permissions=SERVICE_PERMISSIONS,
        label=SERVICE_LABEL,
    )


def _user_principal_from_bearer(authorization: str | None, db: Session) -> Principal | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    try:
        claims = decode_access_token(token)
    except TokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid access token: {exc}") from exc

    try:
        user_id = int(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Malformed token subject") from exc

    user = db.query(models.User).filter(models.User.id == user_id).one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or deactivated")

    return Principal(
        kind="user",
        permissions=permissions_for(user.role, user.extra_permissions),
        user_id=user.id,
        username=user.username,
        role=user.role,
        label=user.username,
        must_change_password=user.must_change_password,
    )


def get_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Principal:
    principal = _user_principal_from_bearer(authorization, db) or _service_principal_from_key(x_api_key)
    if principal is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.principal = principal
    return principal


def require_permission(permission: Permission):
    """Dependency factory: 403 unless the caller holds `permission`."""

    def _dep(
        request: Request,
        principal: Principal = Depends(get_principal),
        db: Session = Depends(get_db),
    ) -> Principal:
        if not principal.has(permission):
            audit.record(
                db,
                actor=principal.actor,
                actor_label=principal.label,
                action=audit.PERMISSION_DENIED,
                target=f"{request.method} {request.url.path}",
                outcome="denied",
                ip=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                detail={"required": permission.value, "held": sorted(p.value for p in principal.permissions)},
            )
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Requires the {permission.value} permission",
            )
        return principal

    return _dep


def require_ingest(
    request: Request,
    principal: Principal = Depends(get_principal),
) -> Principal:
    """Telemetry-ingestion routes (POST heartbeat/logs/trades/positions/pnl).
    Allowed for the machine service identity, or a user with
    TRADING_CONTROL (manual backfill / testing)."""
    if principal.kind == "service" or principal.has(Permission.TRADING_CONTROL):
        return principal
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Telemetry ingestion is not permitted for this identity")


def get_current_user(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.kind != "user":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This endpoint requires a signed-in user")
    return principal


# --------------------------------------------------------------------------
# Rate limiting (DB-backed fixed window, per principal subject)
# --------------------------------------------------------------------------
def _subject_hash(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


def _consume(db: Session, subject_hash: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds)."""
    now = datetime.now(timezone.utc)
    window_epoch = (int(now.timestamp()) // window_seconds) * window_seconds
    window_start = datetime.fromtimestamp(window_epoch, tz=timezone.utc)

    row = (
        db.query(models.RateLimitWindow)
        .filter(
            models.RateLimitWindow.api_key_hash == subject_hash,
            models.RateLimitWindow.window_start == window_start,
        )
        .one_or_none()
    )
    if row is None:
        try:
            db.add(
                models.RateLimitWindow(
                    api_key_hash=subject_hash, window_start=window_start, request_count=1
                )
            )
            db.commit()
            return True, 0
        except IntegrityError:
            db.rollback()
            row = (
                db.query(models.RateLimitWindow)
                .filter(
                    models.RateLimitWindow.api_key_hash == subject_hash,
                    models.RateLimitWindow.window_start == window_start,
                )
                .one()
            )

    if row.request_count >= max_requests:
        return False, max(window_seconds - int(now.timestamp() - window_epoch), 1)

    row.request_count += 1
    db.commit()
    return True, 0


def enforce_rate_limit(
    request: Request,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> None:
    """Per-identity fixed-window limiter. Runs after get_principal, so an
    unauthenticated request is already rejected before it counts."""
    subject = principal.actor
    allowed, retry_after = _consume(
        db, _subject_hash(subject), RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS
    )
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Rate limit exceeded: {RATE_LIMIT_MAX_REQUESTS} requests per {RATE_LIMIT_WINDOW_SECONDS}s",
            headers={"Retry-After": str(retry_after)},
        )


def enforce_login_rate_limit(db: Session, username: str, ip: str) -> None:
    """Brute-force guard for POST /api/auth/login, keyed by username+ip."""
    settings = load_settings()
    subject_hash = _subject_hash(f"login:{username.lower()}:{ip}")
    allowed, retry_after = _consume(
        db, subject_hash, settings.auth_login_max_attempts, settings.auth_login_window_seconds
    )
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed sign-in attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
