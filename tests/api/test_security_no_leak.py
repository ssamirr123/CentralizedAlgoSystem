"""Stage 20 -- API responses / audit rows never carry a credential value."""
from __future__ import annotations

import pytest

from trading.core.config import load_settings
from trading.market_data.providers.base import ProviderAuthError
from trading.market_data.session import BreezeSessionManager, set_session_manager

PLANTED = "PLANTED-SECRET-abc123XYZ"          # a real-looking value that must never echo back
SECRET_KEYS = ('"session_token"', '"api_key"', '"secret_key"', '"api_secret"',
               '"password_hash"', '"totp"', '"refresh_token"', '"client_secret"')


class _FakeProvider:
    def __init__(self, **_):
        pass

    def connect(self):
        raise ProviderAuthError(f"bad token {PLANTED} rejected")  # deliberately leaky

    def disconnect(self):
        pass


@pytest.fixture
def breeze_session(monkeypatch):
    monkeypatch.setenv("BREEZE_API_KEY", "k")
    monkeypatch.setenv("BREEZE_SECRET_KEY", PLANTED)
    monkeypatch.setenv("BREEZE_SESSION_TOKEN", PLANTED)
    monkeypatch.setenv("MARKET_DATA_ENABLED", "true")
    mgr = BreezeSessionManager(
        load_settings(),
        provider_factory=lambda name, **kw: _FakeProvider(**kw),
        secrets_loader=lambda *a, **k: {},
    )
    set_session_manager(mgr)
    yield
    set_session_manager(None)


def _no_secret_keys(text: str):
    low = text.lower()
    for k in SECRET_KEYS:
        assert k not in low, f"response body exposes {k}"
    assert PLANTED not in text and PLANTED.lower() not in low


def test_market_session_status_hides_token(client, viewer_auth, breeze_session):
    r = client.get("/api/market/session/status", headers=viewer_auth)
    assert r.status_code == 200
    _no_secret_keys(r.text)
    creds = r.json()["credentials"]
    assert set(creds) == {"source", "api_key_set", "secret_key_set",
                          "session_token_set", "session_token_fingerprint"}
    assert isinstance(creds["session_token_set"], bool)
    assert PLANTED not in (r.json().get("last_error") or "")


def test_market_health_hides_secrets(client, viewer_auth, breeze_session):
    r = client.get("/api/market/health", headers=viewer_auth)
    assert r.status_code == 200
    _no_secret_keys(r.text)


def test_post_session_response_and_audit_hide_token(client, admin_auth, breeze_session, db_session):
    from trading.database import models

    r = client.post("/api/market/session", json={"session_token": PLANTED}, headers=admin_auth)
    assert r.status_code == 200
    _no_secret_keys(r.text)
    row = (db_session.query(models.AuditLog)
           .filter(models.AuditLog.action == "MARKET_SESSION_UPDATED")
           .order_by(models.AuditLog.id.desc()).first())
    assert row is not None
    assert PLANTED not in str(row.detail)
    assert row.detail.get("session_token_fingerprint")


def test_auth_me_has_no_secret(client, viewer_auth):
    r = client.get("/api/auth/me", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"id", "username", "email", "role", "permissions", "must_change_password"}
    assert "password_hash" not in r.text.lower()
    assert "hash" not in r.text.lower()


def test_login_response_has_no_password_or_signing_secret(client, make_user):
    make_user(username="leaktest", role="viewer", password="Testpass-123!")
    r = client.post("/api/auth/login", json={"username": "leaktest", "password": "Testpass-123!"})
    assert r.status_code == 200
    body = r.text
    assert "Testpass-123!" not in body
    assert "password_hash" not in body.lower()
    s = load_settings()
    if s.auth_secret_key:
        assert s.auth_secret_key not in body


def test_health_endpoint_no_dsn_password(client):
    r = client.get("/api/health")
    assert "@" not in r.text  # a DSN with credentials would contain '@'
