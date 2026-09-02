"""Phase 3 -- /api/market/session endpoints: RBAC + the token is write-only."""
from __future__ import annotations

import pytest

from trading.core.config import load_settings
from trading.database import models
from trading.market_data.providers.base import ProviderAuthError
from trading.market_data.session import BreezeSessionManager, set_session_manager

_TOKEN = "POSTED-DAILY-TOKEN-9f9f9f"


class _FakeProvider:
    def __init__(self, *, mode, **_):
        self._mode = mode

    def connect(self):
        if self._mode == "auth":
            raise ProviderAuthError("expired token blah")

    def disconnect(self):
        pass


@pytest.fixture
def fake_session(monkeypatch):
    monkeypatch.setenv("BREEZE_API_KEY", "k")
    monkeypatch.setenv("BREEZE_SECRET_KEY", "s")
    monkeypatch.setenv("MARKET_DATA_ENABLED", "true")
    monkeypatch.setenv("BREEZE_SESSION_TOKEN", "")
    state = {"mode": "ok"}
    mgr = BreezeSessionManager(
        load_settings(),
        provider_factory=lambda name, **kw: _FakeProvider(mode=state["mode"], **kw),
        secrets_loader=lambda *a, **k: {},
    )
    set_session_manager(mgr)
    yield state
    set_session_manager(None)


def test_status_requires_auth(client, fake_session):
    assert client.get("/api/market/session/status").status_code == 401


def test_viewer_can_read_status(client, viewer_auth, fake_session):
    r = client.get("/api/market/session/status", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "icici_breeze"
    # credentials block exposes only booleans + source + fingerprint -- no value
    assert set(body["credentials"]) == {
        "source", "api_key_set", "secret_key_set", "session_token_set", "session_token_fingerprint"
    }
    assert set(body) == {
        "provider", "enabled", "session_state", "feed_state", "credentials",
        "last_session_check", "last_error",
    }


def test_viewer_cannot_post_session(client, viewer_auth, fake_session):
    r = client.post("/api/market/session", json={"session_token": _TOKEN}, headers=viewer_auth)
    assert r.status_code == 403


def test_operator_cannot_post_session(client, auth, fake_session):
    # `auth` == operator (VIEW+START+STOP+RESTART+TRADING_CONTROL) -- still not ADMIN
    r = client.post("/api/market/session", json={"session_token": _TOKEN}, headers=auth)
    assert r.status_code == 403


def test_admin_posts_session_valid(client, admin_auth, fake_session, db_session):
    r = client.post("/api/market/session", json={"session_token": _TOKEN}, headers=admin_auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_state"] == "VALID"
    assert body["feed_state"] in ("STOPPED", "CONNECTING", "RUNNING")
    # the token must never come back
    assert _TOKEN not in r.text
    assert body["credentials"]["session_token_set"] is True
    assert len(body["credentials"]["session_token_fingerprint"]) == 12

    row = (
        db_session.query(models.AuditLog)
        .filter(models.AuditLog.action == "MARKET_SESSION_UPDATED")
        .order_by(models.AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.target == "breeze:session"
    assert _TOKEN not in str(row.detail)
    assert row.detail["session_token_fingerprint"]
    assert row.detail["result"] == "VALID"


def test_admin_post_session_invalid_token_reports_session_required(client, admin_auth, fake_session):
    fake_session["mode"] = "auth"
    r = client.post("/api/market/session", json={"session_token": "whatever"}, headers=admin_auth)
    assert r.status_code == 200
    assert r.json()["session_state"] == "SESSION_REQUIRED"


def test_post_session_rejects_empty_token(client, admin_auth, fake_session):
    r = client.post("/api/market/session", json={"session_token": ""}, headers=admin_auth)
    assert r.status_code == 422
