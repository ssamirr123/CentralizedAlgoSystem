"""
Central configuration loader for the trading runtime.

Everything is sourced from environment variables — never hard-code
credentials, symbols, or schedules here. See trading/.env.example for the
full list of supported variables.

TRADING_MODE is the single safety gate that every broker adapter must
respect: real order-placement methods must refuse to run unless this is
exactly "live". Defaults to "paper" so a missing/misconfigured env var
fails safe (no live orders), never fails open.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class BrokerCredentials:
    """Raw credential fields, read once at startup. Never logged."""

    zerodha_api_key: str = field(default_factory=lambda: _env("ZERODHA_API_KEY"))
    zerodha_api_secret: str = field(default_factory=lambda: _env("ZERODHA_API_SECRET"))
    zerodha_access_token: str = field(default_factory=lambda: _env("ZERODHA_ACCESS_TOKEN"))

    angelone_api_key: str = field(default_factory=lambda: _env("ANGELONE_API_KEY"))
    angelone_client_id: str = field(default_factory=lambda: _env("ANGELONE_CLIENT_ID"))
    angelone_password: str = field(default_factory=lambda: _env("ANGELONE_PASSWORD"))
    angelone_totp_secret: str = field(default_factory=lambda: _env("ANGELONE_TOTP_SECRET"))

    icici_breeze_api_key: str = field(default_factory=lambda: _env("ICICI_BREEZE_API_KEY"))
    icici_breeze_api_secret: str = field(default_factory=lambda: _env("ICICI_BREEZE_API_SECRET"))
    icici_breeze_session_token: str = field(default_factory=lambda: _env("ICICI_BREEZE_SESSION_TOKEN"))


@dataclass(frozen=True)
class TradingConfig:
    # Safety gate — must be the literal string "live" to allow real orders.
    trading_mode: str = field(default_factory=lambda: _env("TRADING_MODE", "paper").lower())

    # Which BrokerClient implementation to instantiate: paper | zerodha | angelone | icici_breeze
    broker_name: str = field(default_factory=lambda: _env("BROKER", "paper").lower())

    # Identity + control-center API base URL used for heartbeat / log /
    # P&L reporting. API_BASE_URL is injected per-run by the orchestrator
    # (START_ALGO) in production.
    strategy_name: str = field(default_factory=lambda: _env("STRATEGY_NAME", "example_strategy"))
    server_name: str = field(default_factory=lambda: _env("SERVER_NAME", "local-dev"))
    api_base_url: str = field(default_factory=lambda: _env("API_BASE_URL", "http://127.0.0.1:8000"))
    heartbeat_interval_seconds: int = field(
        default_factory=lambda: _env_int("HEARTBEAT_INTERVAL_SECONDS", 30)
    )

    # Control-center heartbeat (POST /api/heartbeat) -- separate interval
    # from the line above (project's own recommended default is 10s here,
    # vs. the old dashboard's 30s, which staleness math elsewhere depends on).
    control_api_key: str = field(default_factory=lambda: _env("CONTROL_API_KEY"))
    control_heartbeat_interval_seconds: int = field(
        default_factory=lambda: _env_int("CONTROL_HEARTBEAT_INTERVAL_SECONDS", 10)
    )

    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())

    broker_reconnect_max_attempts: int = field(
        default_factory=lambda: _env_int("BROKER_RECONNECT_MAX_ATTEMPTS", 5)
    )
    broker_reconnect_backoff_seconds: float = field(
        default_factory=lambda: _env_float("BROKER_RECONNECT_BACKOFF_SECONDS", 2.0)
    )

    credentials: BrokerCredentials = field(default_factory=BrokerCredentials)

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"


def load_config() -> TradingConfig:
    """Load configuration fresh from the current environment."""
    return TradingConfig()
