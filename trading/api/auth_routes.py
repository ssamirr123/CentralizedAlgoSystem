"""
Human authentication: username/password -> JWT access token, with an
httpOnly refresh cookie.

    POST /api/auth/login             {username, password}      -> access token (+ cookies)
    POST /api/auth/refresh           cookie + X-CSRF-Token      -> new access token (rotates)
    POST /api/auth/logout            cookie + X-CSRF-Token      -> revoke session
    GET  /api/auth/me                Bearer                     -> current identity + permissions
    POST /api/auth/change-password   Bearer {current,new}       -> rotate password, revoke other sessions

Access tokens are Bearer-only (never a cookie) so they are not
CSRF-exploitable. The refresh token is an opaque value stored only as a
SHA-256 hash; the cookie is httpOnly + Secure + SameSite=Strict and
scoped to /api/auth. Refresh/logout additionally require a double-submit
CSRF header.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from trading.api.deps import (
    Principal,
    client_ip,
    enforce_login_rate_limit,
    get_current_user,
    get_db,
)
from trading.api.security import audit
from trading.api.security.passwords import (
    WeakPasswordError,
    hash_password,
    validate_password_strength,
    verify_password,
)
from trading.api.security.permissions import permissions_for
from trading.api.security.tokens import (
    create_access_token,
    hash_refresh_token,
    new_csrf_token,
    new_refresh_token,
    refresh_expiry,
)
from trading.core.config import load_settings
from trading.database import models

logger = logging.getLogger("trading.api.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "cas_refresh"
CSRF_COOKIE = "cas_csrf"
REFRESH_PATH = "/api/auth"


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: int
    username: str
    email: str | None
    role: str
    permissions: list[str]
    must_change_password: bool


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _user_out(user: models.User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        permissions=sorted(p.value for p in permissions_for(user.role, user.extra_permissions)),
        must_change_password=user.must_change_password,
    )


def _set_auth_cookies(response: Response, refresh_raw: str, csrf: str) -> None:
    settings = load_settings()
    secure = settings.auth_cookie_secure
    domain = settings.auth_cookie_domain or None
    max_age = settings.auth_refresh_ttl_days * 24 * 3600
    response.set_cookie(
        REFRESH_COOKIE, refresh_raw, max_age=max_age, path=REFRESH_PATH,
        httponly=True, secure=secure, samesite="strict", domain=domain,
    )
    # Readable by JS on purpose (double-submit). Still Secure + Strict.
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=max_age, path="/",
        httponly=False, secure=secure, samesite="strict", domain=domain,
    )


def _clear_auth_cookies(response: Response) -> None:
    domain = load_settings().auth_cookie_domain or None
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH, domain=domain)
    response.delete_cookie(CSRF_COOKIE, path="/", domain=domain)


def _issue_session(
    db: Session, user: models.User, request: Request
) -> tuple[str, str, models.AuthSession]:
    refresh_raw = new_refresh_token()
    csrf = new_csrf_token()
    row = models.AuthSession(
        user_id=user.id,
        token_hash=hash_refresh_token(refresh_raw),
        csrf_token=csrf,
        expires_at=refresh_expiry(),
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:255] or None,
    )
    db.add(row)
    db.flush()
    return refresh_raw, csrf, row


def _as_aware(dt: datetime | None) -> datetime | None:
    # SQLite (local/dev/tests) drops tzinfo on DateTime(timezone=True);
    # Postgres keeps it. Normalise so comparisons never raise.
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _active_session(db: Session, refresh_raw: str) -> models.AuthSession | None:
    row = (
        db.query(models.AuthSession)
        .filter(models.AuthSession.token_hash == hash_refresh_token(refresh_raw))
        .one_or_none()
    )
    if row is None or row.revoked_at is not None:
        return None
    if _as_aware(row.expires_at) <= datetime.now(timezone.utc):
        return None
    return row


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> TokenOut:
    ip = client_ip(request)
    ua = request.headers.get("user-agent")
    enforce_login_rate_limit(db, body.username, ip)

    user = db.query(models.User).filter(models.User.username == body.username).one_or_none()
    ok = user is not None and user.is_active and verify_password(body.password, user.password_hash)
    if not ok:
        audit.record(
            db, actor=(f"user:{user.id}" if user else "anonymous"), actor_label=body.username,
            action=audit.AUTH_LOGIN_FAILED, outcome="denied", ip=ip, user_agent=ua,
            detail={"reason": "bad_credentials" if user else "unknown_user"},
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")

    refresh_raw, csrf, _ = _issue_session(db, user, request)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    perms = sorted(p.value for p in permissions_for(user.role, user.extra_permissions))
    access, expires_in = create_access_token(
        user_id=user.id, username=user.username, role=user.role, permissions=perms
    )
    _set_auth_cookies(response, refresh_raw, csrf)
    audit.record(
        db, actor=f"user:{user.id}", actor_label=user.username, action=audit.AUTH_LOGIN,
        ip=ip, user_agent=ua,
    )
    return TokenOut(access_token=access, expires_in=expires_in, user=_user_out(user))


@router.post("/refresh", response_model=TokenOut)
def refresh(
    request: Request,
    response: Response,
    x_csrf_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> TokenOut:
    ip = client_ip(request)
    ua = request.headers.get("user-agent")
    refresh_raw = request.cookies.get(REFRESH_COOKIE)
    csrf_cookie = request.cookies.get(CSRF_COOKIE)

    def _deny(reason: str):
        audit.record(
            db, actor="anonymous", action=audit.AUTH_REFRESH_DENIED, outcome="denied",
            ip=ip, user_agent=ua, detail={"reason": reason},
        )
        _clear_auth_cookies(response)
        return HTTPException(status.HTTP_401_UNAUTHORIZED, "Could not refresh session")

    if not refresh_raw:
        raise _deny("no_cookie")
    # Double-submit CSRF: header must equal the non-httpOnly cookie.
    if not x_csrf_token or not csrf_cookie or x_csrf_token != csrf_cookie:
        raise _deny("csrf_mismatch")

    session = _active_session(db, refresh_raw)
    if session is None or session.csrf_token != x_csrf_token:
        raise _deny("invalid_session")

    user = db.query(models.User).filter(models.User.id == session.user_id).one_or_none()
    if user is None or not user.is_active:
        raise _deny("user_inactive")

    # Rotate.
    new_raw, new_csrf, new_row = _issue_session(db, user, request)
    session.revoked_at = datetime.now(timezone.utc)
    session.rotated_to = new_row.id
    db.commit()

    perms = sorted(p.value for p in permissions_for(user.role, user.extra_permissions))
    access, expires_in = create_access_token(
        user_id=user.id, username=user.username, role=user.role, permissions=perms
    )
    _set_auth_cookies(response, new_raw, new_csrf)
    audit.record(db, actor=f"user:{user.id}", actor_label=user.username, action=audit.AUTH_REFRESH, ip=ip, user_agent=ua)
    return TokenOut(access_token=access, expires_in=expires_in, user=_user_out(user))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    x_csrf_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Response:
    refresh_raw = request.cookies.get(REFRESH_COOKIE)
    csrf_cookie = request.cookies.get(CSRF_COOKIE)
    if refresh_raw and x_csrf_token and csrf_cookie and x_csrf_token == csrf_cookie:
        session = _active_session(db, refresh_raw)
        if session is not None and session.csrf_token == x_csrf_token:
            session.revoked_at = datetime.now(timezone.utc)
            db.commit()
            audit.record(
                db, actor=f"user:{session.user_id}", action=audit.AUTH_LOGOUT,
                ip=client_ip(request), user_agent=request.headers.get("user-agent"),
            )
    _clear_auth_cookies(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserOut)
def me(user: Principal = Depends(get_current_user), db: Session = Depends(get_db)) -> UserOut:
    row = db.query(models.User).filter(models.User.id == user.user_id).one()
    return _user_out(row)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordIn,
    request: Request,
    user: Principal = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    row = db.query(models.User).filter(models.User.id == user.user_id).one()
    if not verify_password(body.current_password, row.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    try:
        validate_password_strength(body.new_password)
    except WeakPasswordError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if verify_password(body.new_password, row.password_hash):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "New password must differ from the current one")

    row.password_hash = hash_password(body.new_password)
    row.must_change_password = False
    # Invalidate every other session for this user.
    db.query(models.AuthSession).filter(
        models.AuthSession.user_id == row.id, models.AuthSession.revoked_at.is_(None)
    ).update({models.AuthSession.revoked_at: datetime.now(timezone.utc)})
    db.commit()
    audit.record(
        db, actor=f"user:{row.id}", actor_label=row.username, action=audit.AUTH_PASSWORD_CHANGED,
        ip=client_ip(request), user_agent=request.headers.get("user-agent"),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
