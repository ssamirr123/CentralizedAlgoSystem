"""Access tokens (short-lived signed JWT) and refresh tokens (opaque,
stored only as a hash).

The access token is sent in the response body and carried by the browser
in memory + the Authorization header -- never a cookie -- so it is immune
to CSRF. The refresh token lives only in an httpOnly SameSite=Strict
cookie; the server keeps just its SHA-256 hash.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt

from trading.core.config import load_settings

ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"


class TokenError(Exception):
    pass


def _secret() -> str:
    key = load_settings().auth_secret_key
    if not key:
        raise TokenError("AUTH_SECRET_KEY is not configured")
    return key


def create_access_token(*, user_id: int, username: str, role: str, permissions: list[str]) -> tuple[str, int]:
    """Return (jwt, expires_in_seconds)."""
    settings = load_settings()
    ttl = timedelta(minutes=settings.auth_access_ttl_minutes)
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "perms": permissions,
        "type": ACCESS_TOKEN_TYPE,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM), int(ttl.total_seconds())


def decode_access_token(token: str) -> dict:
    try:
        claims = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid token") from exc
    if claims.get("type") != ACCESS_TOKEN_TYPE:
        raise TokenError("wrong token type")
    return claims


def new_refresh_token() -> str:
    """A fresh opaque refresh token (the raw value handed to the client)."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def refresh_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=load_settings().auth_refresh_ttl_days)
