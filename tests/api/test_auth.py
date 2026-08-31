"""Stage 18 -- authentication: login, JWT, refresh cookie + CSRF, logout,
password change, bootstrap, login rate limiting."""
from __future__ import annotations

import pytest

from trading.api.auth_routes import CSRF_COOKIE, REFRESH_COOKIE
from trading.api.security.passwords import hash_password
from trading.database import models

PW = "Testpass-123!"


@pytest.fixture
def user(db_session):
    u = models.User(username="alice", password_hash=hash_password(PW), role="viewer")
    db_session.add(u)
    db_session.commit()
    return u


def test_login_success_returns_token_and_sets_cookies(client, user):
    r = client.post("/api/auth/login", json={"username": "alice", "password": PW})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["role"] == "viewer"
    assert body["user"]["permissions"] == ["VIEW"]
    assert REFRESH_COOKIE in r.cookies
    assert CSRF_COOKIE in r.cookies


def test_login_bad_password_401_and_audited(client, user, db_session):
    r = client.post("/api/auth/login", json={"username": "alice", "password": "nope"})
    assert r.status_code == 401
    rows = db_session.query(models.AuditLog).filter(models.AuditLog.action == "AUTH_LOGIN_FAILED").all()
    assert len(rows) == 1
    assert rows[0].outcome == "denied"


def test_login_unknown_user_401(client):
    assert client.post("/api/auth/login", json={"username": "ghost", "password": PW}).status_code == 401


def test_inactive_user_cannot_login(client, user, db_session):
    user.is_active = False
    db_session.commit()
    assert client.post("/api/auth/login", json={"username": "alice", "password": PW}).status_code == 401


def test_me_requires_bearer(client, user):
    assert client.get("/api/auth/me").status_code == 401
    tok = client.post("/api/auth/login", json={"username": "alice", "password": PW}).json()["access_token"]
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and r.json()["username"] == "alice"


def test_service_key_cannot_use_user_only_endpoint(client, api_key):
    # /api/auth/me is user-only; the machine key is not a user.
    assert client.get("/api/auth/me", headers={"X-API-Key": api_key}).status_code == 403


def test_refresh_requires_matching_csrf(client, user):
    client.post("/api/auth/login", json={"username": "alice", "password": PW})
    csrf = client.cookies.get(CSRF_COOKIE)
    assert client.post("/api/auth/refresh").status_code == 401
    assert client.post("/api/auth/refresh", headers={"X-CSRF-Token": "wrong"}).status_code == 401
    assert client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf}).status_code == 200


def test_refresh_rotates_and_old_token_dies(client, user, db_session):
    client.post("/api/auth/login", json={"username": "alice", "password": PW})
    old_refresh = client.cookies.get(REFRESH_COOKIE)
    csrf = client.cookies.get(CSRF_COOKIE)
    assert client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf}).status_code == 200
    # Old refresh cookie value is now revoked.
    client.cookies.set(REFRESH_COOKIE, old_refresh)
    assert client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf}).status_code == 401


def test_logout_revokes_session(client, user):
    client.post("/api/auth/login", json={"username": "alice", "password": PW})
    csrf = client.cookies.get(CSRF_COOKIE)
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf}).status_code == 401


def test_change_password_enforces_strength_and_rotates(client, user):
    tok = client.post("/api/auth/login", json={"username": "alice", "password": PW}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert client.post("/api/auth/change-password", headers=h,
                       json={"current_password": PW, "new_password": "weak"}).status_code == 422
    assert client.post("/api/auth/change-password", headers=h,
                       json={"current_password": "wrong", "new_password": "An0ther-Str0ng!"}).status_code == 400
    assert client.post("/api/auth/change-password", headers=h,
                       json={"current_password": PW, "new_password": "An0ther-Str0ng!"}).status_code == 204
    assert client.post("/api/auth/login", json={"username": "alice", "password": PW}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "alice", "password": "An0ther-Str0ng!"}).status_code == 200


def test_login_rate_limited_after_repeated_failures(client, user, monkeypatch):
    # enforce_login_rate_limit reads settings fresh via load_settings().
    monkeypatch.setenv("AUTH_LOGIN_MAX_ATTEMPTS", "3")
    for _ in range(3):
        client.post("/api/auth/login", json={"username": "alice", "password": "bad"})
    r = client.post("/api/auth/login", json={"username": "alice", "password": "bad"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_expired_access_token_rejected(client, user, monkeypatch):
    monkeypatch.setenv("AUTH_ACCESS_TTL_MINUTES", "-1")  # already expired
    tok = client.post("/api/auth/login", json={"username": "alice", "password": PW}).json()["access_token"]
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
