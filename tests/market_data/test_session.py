"""Phase 3 -- Breeze session manager (never leaks a token)."""
from __future__ import annotations

import pytest

from trading.core.config import load_settings
from trading.market_data.providers.base import ProviderAuthError, ProviderConnectionError
from trading.market_data.session import BreezeSessionManager
from trading.market_data.status import FEED_STATUS, SessionState

_TOKEN = "DAILY-SESSION-abc123"
_SECRET = "app-secret-xyz"


class _FakeProvider:
    def __init__(self, *, mode="ok", **_):
        self._mode = mode

    def connect(self):
        if self._mode == "auth":
            raise ProviderAuthError("session token expired: bad-token-value")
        if self._mode == "net":
            raise ProviderConnectionError("connection refused")

    def disconnect(self):
        pass


def _mgr(monkeypatch, *, mode="ok", **env):
    for k, v in {"BREEZE_API_KEY": "", "BREEZE_SECRET_KEY": "", "BREEZE_SESSION_TOKEN": ""}.items():
        monkeypatch.setenv(k, env.get(k, v))
    monkeypatch.setenv("MARKET_DATA_ENABLED", env.get("MARKET_DATA_ENABLED", "true"))
    return BreezeSessionManager(
        load_settings(),
        provider_factory=lambda name, **kw: _FakeProvider(mode=mode, **kw),
        secrets_loader=lambda sid, region: (_ for _ in ()).throw(AssertionError("should not read SM")),
    )


def test_not_configured_when_no_app_creds(monkeypatch):
    m = _mgr(monkeypatch)
    chk = m.check()
    assert chk.state is SessionState.NOT_CONFIGURED
    assert m.state() is SessionState.NOT_CONFIGURED


def test_session_required_when_token_missing(monkeypatch):
    m = _mgr(monkeypatch, BREEZE_API_KEY="k", BREEZE_SECRET_KEY=_SECRET)
    assert m.check().state is SessionState.SESSION_REQUIRED


def test_valid_session(monkeypatch):
    m = _mgr(monkeypatch, BREEZE_API_KEY="k", BREEZE_SECRET_KEY=_SECRET, BREEZE_SESSION_TOKEN=_TOKEN, mode="ok")
    chk = m.check()
    assert chk.state is SessionState.VALID
    snap = FEED_STATUS.snapshot()
    assert snap["session_state"] == "VALID"
    assert snap["credentials"]["session_token_set"] is True
    # token itself never present anywhere in the status
    assert _TOKEN not in str(snap)
    assert snap["credentials"]["session_token_fingerprint"] and len(snap["credentials"]["session_token_fingerprint"]) == 12


def test_provider_auth_error_maps_to_session_required_and_scrubs_message(monkeypatch):
    m = _mgr(monkeypatch, BREEZE_API_KEY="k", BREEZE_SECRET_KEY=_SECRET, BREEZE_SESSION_TOKEN=_TOKEN, mode="auth")
    chk = m.check()
    assert chk.state is SessionState.SESSION_REQUIRED
    assert "bad-token-value" not in (chk.detail or "")


def test_provider_connection_error_maps_to_error(monkeypatch):
    m = _mgr(monkeypatch, BREEZE_API_KEY="k", BREEZE_SECRET_KEY=_SECRET, BREEZE_SESSION_TOKEN=_TOKEN, mode="net")
    assert m.check().state is SessionState.ERROR


def test_runtime_override_takes_precedence_over_env(monkeypatch):
    m = _mgr(monkeypatch, BREEZE_API_KEY="k", BREEZE_SECRET_KEY=_SECRET, BREEZE_SESSION_TOKEN="OLD", mode="ok")
    m.set_credentials(session_token="NEW-RUNTIME-TOKEN")
    creds = m.credentials()
    assert creds.session_token == "NEW-RUNTIME-TOKEN"
    assert creds.source == "runtime"
    red = creds.redacted()
    assert "NEW-RUNTIME-TOKEN" not in str(red)
    assert red["session_token_set"] is True


def test_redacted_never_contains_secret_values(monkeypatch):
    m = _mgr(monkeypatch, BREEZE_API_KEY="APIKEY123", BREEZE_SECRET_KEY=_SECRET, BREEZE_SESSION_TOKEN=_TOKEN)
    red = m.credentials().redacted()
    blob = str(red)
    for v in ("APIKEY123", _SECRET, _TOKEN):
        assert v not in blob
    assert red == {
        "source": "env",
        "api_key_set": True,
        "secret_key_set": True,
        "session_token_set": True,
        "session_token_fingerprint": red["session_token_fingerprint"],
    }


def test_secrets_loader_used_when_secret_id_set(monkeypatch):
    monkeypatch.setenv("BREEZE_SECRET_ID", "arn:aws:secretsmanager:...:breeze")
    monkeypatch.setenv("MARKET_DATA_ENABLED", "true")
    for k in ("BREEZE_API_KEY", "BREEZE_SECRET_KEY", "BREEZE_SESSION_TOKEN"):
        monkeypatch.setenv(k, "")

    loaded = {"api_key": "sm-key", "secret_key": "sm-secret", "session_token": "sm-token"}
    m = BreezeSessionManager(
        load_settings(),
        provider_factory=lambda name, **kw: _FakeProvider(mode="ok", **kw),
        secrets_loader=lambda sid, region: loaded,
    )
    creds = m.credentials()
    assert creds.source == "secrets_manager"
    assert creds.is_complete
    assert m.check().state is SessionState.VALID
