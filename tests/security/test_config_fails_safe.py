"""Stage 20 -- missing credentials fail safely (empty, not a crash / not a literal)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ALGOS = ["CombinedVwapNifty", "DoubleStraddelAlgo", "Vwap_Algo_Nifty_hedge"]


@pytest.mark.parametrize("algo", ALGOS)
def test_algo_config_empty_when_env_unset(algo, monkeypatch):
    for k in ("ANGELONE_API_KEY", "ANGELONE_CLIENT_ID", "ANGELONE_MPIN",
              "ANGELONE_PASSWORD", "ANGELONE_TOTP_SECRET"):
        monkeypatch.delenv(k, raising=False)
    algo_dir = ROOT / "trading" / "algos" / algo
    monkeypatch.syspath_prepend(str(algo_dir))
    sys.modules.pop("config", None)
    cfg = importlib.import_module("config")
    try:
        assert cfg.clientid == "" and cfg.apikey == "" and cfg.mpin == "" and cfg.token == ""
    finally:
        sys.modules.pop("config", None)


@pytest.mark.parametrize("algo", ALGOS)
def test_algo_config_populates_from_env(algo, monkeypatch):
    monkeypatch.setenv("ANGELONE_CLIENT_ID", "CID_FAKE")
    monkeypatch.setenv("ANGELONE_API_KEY", "KEY_FAKE")
    monkeypatch.setenv("ANGELONE_MPIN", "0000")
    monkeypatch.setenv("ANGELONE_TOTP_SECRET", "TOTP_FAKE")
    algo_dir = ROOT / "trading" / "algos" / algo
    monkeypatch.syspath_prepend(str(algo_dir))
    sys.modules.pop("config", None)
    cfg = importlib.import_module("config")
    try:
        assert (cfg.clientid, cfg.apikey, cfg.mpin, cfg.token) == ("CID_FAKE", "KEY_FAKE", "0000", "TOTP_FAKE")
    finally:
        sys.modules.pop("config", None)


def test_breeze_provider_connect_fails_safe_without_creds():
    from trading.market_data.providers import ICICIBreezeProvider, ProviderAuthError

    p = ICICIBreezeProvider(api_key="", api_secret="", session_token="",
                            client_factory=lambda k: object())
    with pytest.raises(ProviderAuthError):
        p.connect()
    assert p.is_connected() is False


def test_settings_breeze_fields_blank_when_unset(monkeypatch):
    for k in ("BREEZE_API_KEY", "BREEZE_SECRET_KEY", "BREEZE_SESSION_TOKEN",
              "ICICI_BREEZE_API_KEY", "ICICI_BREEZE_API_SECRET", "ICICI_BREEZE_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    from trading.core.config import Settings

    s = Settings()
    assert s.breeze_api_key == "" and s.breeze_secret_key == "" and s.breeze_session_token == ""
    assert s.is_live is False  # paper mode preserved
