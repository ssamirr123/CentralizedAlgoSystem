"""Centralized configuration layer -- trading/core/config.py (Stage 9)."""
from __future__ import annotations

import pytest

from trading.core.config import Settings, load_settings

_ENV_KEYS = [
    "APP_ENV", "DATABASE_URL", "LOG_LEVEL",
    "TRADING_MODE", "BROKER",
    "STRATEGY_NAME", "SERVER_NAME", "API_BASE_URL",
    "HEARTBEAT_INTERVAL_SECONDS", "CONTROL_HEARTBEAT_INTERVAL_SECONDS",
    "CONTROL_API_KEY", "RATE_LIMIT_MAX_REQUESTS", "RATE_LIMIT_WINDOW_SECONDS",
    "STALE_THRESHOLD_MINUTES", "STALE_CHECK_INTERVAL_SECONDS",
    "DISABLE_BACKGROUND_WATCHER", "DAY_LOSS_LIMIT",
    "AWS_REGION", "LAMBDA_FUNCTION_NAME",
    "BROKER_RECONNECT_MAX_ATTEMPTS", "BROKER_RECONNECT_BACKOFF_SECONDS",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "ALERT_DEDUP_SECONDS", "ALERT_MAX_RETRIES", "ALERT_RETRY_DELAY",
    "ZERODHA_API_KEY", "ZERODHA_API_SECRET", "ZERODHA_ACCESS_TOKEN",
    "ANGELONE_API_KEY", "ANGELONE_CLIENT_ID", "ANGELONE_PASSWORD", "ANGELONE_TOTP_SECRET",
    "ICICI_BREEZE_API_KEY", "ICICI_BREEZE_API_SECRET", "ICICI_BREEZE_SESSION_TOKEN",
]


@pytest.fixture
def clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


# --------------------------- defaults ---------------------------
def test_defaults(clean_env):
    s = load_settings()
    assert s.trading_mode == "paper"
    assert s.is_live is False
    assert s.broker_name == "paper"
    assert s.app_env == "development"
    assert s.log_level == "INFO"
    assert s.control_api_key == ""
    assert s.rate_limit_max_requests == 60
    assert s.rate_limit_window_seconds == 60
    assert s.stale_threshold_minutes == 2.0
    assert s.stale_check_interval_seconds == 60
    assert s.disable_background_watcher is False
    assert s.day_loss_limit == 10000.0
    assert s.aws_region == ""
    assert s.lambda_function_name == ""
    assert s.heartbeat_interval_seconds == 30
    assert s.control_heartbeat_interval_seconds == 10
    assert s.broker_reconnect_max_attempts == 5
    assert s.broker_reconnect_backoff_seconds == 2.0
    assert s.telegram_bot_token == "" and s.telegram_chat_id == ""
    assert s.alert_dedup_seconds == 120
    assert s.alert_max_retries == 3
    assert s.alert_retry_delay == 2.0
    assert s.database_url.startswith("sqlite:///")


def test_settings_is_frozen(clean_env):
    s = load_settings()
    with pytest.raises(Exception):
        s.trading_mode = "live"  # type: ignore[misc]


# --------------------------- paper mode ---------------------------
@pytest.mark.parametrize("value", [None, "paper", "PAPER", "Paper", "  paper  "])
def test_paper_mode(clean_env, value):
    if value is not None:
        clean_env.setenv("TRADING_MODE", value)
    s = load_settings()
    assert s.trading_mode == "paper"
    assert s.is_live is False


# --------------------------- live-mode safety gate ---------------------------
@pytest.mark.parametrize("value, is_live", [
    ("live", True),
    ("LIVE", True),
    ("  live  ", True),
    ("paper", False),
    ("", False),
    ("test", False),
    ("livex", False),
    ("liv", False),
])
def test_live_safety_gate(clean_env, value, is_live):
    clean_env.setenv("TRADING_MODE", value)
    assert load_settings().is_live is is_live


def test_live_requires_exact_value(clean_env):
    """Sanity: the ONLY string that enables live trading is 'live'."""
    for v in ["real", "prod", "production", "1", "true", "yes", "on"]:
        clean_env.setenv("TRADING_MODE", v)
        assert load_settings().is_live is False


# --------------------------- environment overrides ---------------------------
def test_environment_overrides(clean_env):
    overrides = {
        "APP_ENV": "production",
        "TRADING_MODE": "live",
        "BROKER": "zerodha",
        "CONTROL_API_KEY": "secret-key",
        "DATABASE_URL": "postgresql://u:p@db:5432/x",
        "AWS_REGION": "ap-south-1",
        "LAMBDA_FUNCTION_NAME": "TradingOrchestrator",
        "RATE_LIMIT_MAX_REQUESTS": "10",
        "RATE_LIMIT_WINDOW_SECONDS": "30",
        "STALE_THRESHOLD_MINUTES": "5",
        "STALE_CHECK_INTERVAL_SECONDS": "15",
        "DISABLE_BACKGROUND_WATCHER": "true",
        "DAY_LOSS_LIMIT": "25000",
        "HEARTBEAT_INTERVAL_SECONDS": "45",
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_CHAT_ID": "-100999",
        "ALERT_MAX_RETRIES": "7",
    }
    for k, v in overrides.items():
        clean_env.setenv(k, v)
    s = load_settings()
    assert s.app_env == "production"
    assert s.trading_mode == "live" and s.is_live is True
    assert s.broker_name == "zerodha"
    assert s.control_api_key == "secret-key"
    assert s.database_url == "postgresql://u:p@db:5432/x"
    assert s.aws_region == "ap-south-1"
    assert s.lambda_function_name == "TradingOrchestrator"
    assert s.rate_limit_max_requests == 10
    assert s.rate_limit_window_seconds == 30
    assert s.stale_threshold_minutes == 5.0
    assert s.stale_check_interval_seconds == 15
    assert s.disable_background_watcher is True
    assert s.day_loss_limit == 25000.0
    assert s.heartbeat_interval_seconds == 45
    assert s.telegram_bot_token == "123:abc"
    assert s.telegram_chat_id == "-100999"
    assert s.alert_max_retries == 7


@pytest.mark.parametrize("value, expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("Yes", True),
    ("0", False), ("false", False), ("no", False), ("", False), ("maybe", False),
])
def test_disable_background_watcher_parsing(clean_env, value, expected):
    clean_env.setenv("DISABLE_BACKGROUND_WATCHER", value)
    assert load_settings().disable_background_watcher is expected


def test_blank_numeric_env_falls_back_to_default(clean_env):
    clean_env.setenv("RATE_LIMIT_MAX_REQUESTS", "")
    clean_env.setenv("STALE_THRESHOLD_MINUTES", "")
    s = load_settings()
    assert s.rate_limit_max_requests == 60
    assert s.stale_threshold_minutes == 2.0


# --------------------------- missing optional credentials ---------------------------
def test_missing_optional_credentials(clean_env):
    s = load_settings()  # no creds set anywhere
    c = s.credentials
    assert c.zerodha_api_key == "" and c.zerodha_api_secret == "" and c.zerodha_access_token == ""
    assert c.angelone_api_key == "" and c.angelone_client_id == ""
    assert c.angelone_password == "" and c.angelone_totp_secret == ""
    assert c.icici_breeze_api_key == "" and c.icici_breeze_api_secret == ""
    assert c.icici_breeze_session_token == ""
    assert s.telegram_bot_token == "" and s.telegram_chat_id == ""
    # constructing / reading Settings with everything unset must not raise
    assert isinstance(s, Settings)


def test_broker_credentials_populate_from_env(clean_env):
    clean_env.setenv("ZERODHA_API_KEY", "zk")
    clean_env.setenv("ANGELONE_CLIENT_ID", "ac")
    clean_env.setenv("ICICI_BREEZE_SESSION_TOKEN", "it")
    c = load_settings().credentials
    assert c.zerodha_api_key == "zk"
    assert c.angelone_client_id == "ac"
    assert c.icici_breeze_session_token == "it"


# --------------------------- agrees with the legacy runtime config ---------------------------
def test_agrees_with_common_config(clean_env):
    """trading/common/config.py is left in place; the shared fields must
    read identically so the two layers never disagree."""
    from trading.common.config import load_config

    for tm in ["paper", "live", "", "test"]:
        clean_env.setenv("TRADING_MODE", tm)
        clean_env.setenv("BROKER", "angelone")
        s, c = load_settings(), load_config()
        assert s.trading_mode == c.trading_mode
        assert s.is_live == c.is_live
        assert s.broker_name == c.broker_name
        assert s.heartbeat_interval_seconds == c.heartbeat_interval_seconds
        assert s.control_heartbeat_interval_seconds == c.control_heartbeat_interval_seconds
        assert s.broker_reconnect_max_attempts == c.broker_reconnect_max_attempts
        assert s.log_level == c.log_level


# --------------------------- wiring: refactored modules read the same values ---------------------------
def test_refactored_module_constants_unchanged():
    import trading.api.deps as deps
    import trading.api.watcher as watcher

    assert deps.RATE_LIMIT_MAX_REQUESTS == 60
    assert deps.RATE_LIMIT_WINDOW_SECONDS == 60
    assert watcher.STALE_THRESHOLD_MINUTES == 2.0
    assert watcher.STALE_CHECK_INTERVAL_SECONDS == 60
    assert load_settings().day_loss_limit == 10000.0
