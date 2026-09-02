"""
Single centralized configuration layer.

Every backend / control-center setting is read from an environment
variable here, once, in one place. Call ``load_settings()`` to get a
frozen ``Settings`` snapshot of the current environment.

Environment variable names are UNCHANGED from the scattered reads this
replaces -- nothing was renamed. Defaults match the previous inline
``os.environ.get(...)`` defaults exactly, so wiring a module through
``load_settings()`` does not change its behavior.

Safety gate
-----------
``TRADING_MODE`` defaults to ``"paper"``. ``Settings.is_live`` is True
only when it is exactly ``"live"`` (after strip + lowercase). A missing
or misspelled value fails safe (paper), never open.

Scope note
----------
``trading/common/config.py`` (the per-algo runtime ``TradingConfig`` /
``load_config()``) is intentionally left in place and unchanged; the
running strategies still use it. The two layers read the same env vars
with the same defaults for the fields they share, so they never disagree
(``tests/core/test_config.py`` pins this). ``connection.py`` (DATABASE_URL,
builds the engine at import) and ``alerts/telegram.py`` keep their own
reads for now -- both are noted below.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from trading.common.config import BrokerCredentials

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SQLITE_PATH = _PROJECT_ROOT / "trading" / "data" / "control_center.db"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(name: str) -> bool:
    # Matches the previous inline check in trading/api/app.py exactly.
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Settings:
    # --- App / runtime -------------------------------------------------
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "development"))
    # Informational mirror of trading.database.connection.DATABASE_URL --
    # that module still owns the actual engine. Same default expression.
    database_url: str = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", f"sqlite:///{_DEFAULT_SQLITE_PATH}")
    )
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())

    # --- Trading safety gate ----------------------------------------
    trading_mode: str = field(default_factory=lambda: _env("TRADING_MODE", "paper").lower())
    broker_name: str = field(default_factory=lambda: _env("BROKER", "paper").lower())

    # --- Identity / heartbeat reporting ---------------------------
    strategy_name: str = field(default_factory=lambda: _env("STRATEGY_NAME", "example_strategy"))
    server_name: str = field(default_factory=lambda: _env("SERVER_NAME", "local-dev"))
    api_base_url: str = field(default_factory=lambda: _env("API_BASE_URL", "http://127.0.0.1:8000"))
    heartbeat_interval_seconds: int = field(
        default_factory=lambda: _env_int("HEARTBEAT_INTERVAL_SECONDS", 30)
    )
    control_heartbeat_interval_seconds: int = field(
        default_factory=lambda: _env_int("CONTROL_HEARTBEAT_INTERVAL_SECONDS", 10)
    )

    # --- Control-center API --------------------------------------
    control_api_key: str = field(default_factory=lambda: _env("CONTROL_API_KEY"))
    rate_limit_max_requests: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_MAX_REQUESTS", 60)
    )
    rate_limit_window_seconds: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_WINDOW_SECONDS", 60)
    )

    # --- Stage 18: human authentication (username/password -> JWT) ------
    # HS256 signing secret for access tokens. MUST be set (>= 32 chars) in
    # any non-development environment -- deps.py fails closed if it is
    # missing while APP_ENV != development.
    auth_secret_key: str = field(default_factory=lambda: _env("AUTH_SECRET_KEY"))
    auth_access_ttl_minutes: int = field(
        default_factory=lambda: _env_int("AUTH_ACCESS_TTL_MINUTES", 15)
    )
    auth_refresh_ttl_days: int = field(
        default_factory=lambda: _env_int("AUTH_REFRESH_TTL_DAYS", 7)
    )
    # Secure + SameSite=Strict refresh/CSRF cookies. Default true; set
    # AUTH_COOKIE_SECURE=false only for plain-http local dev.
    auth_cookie_secure: bool = field(
        default_factory=lambda: _env("AUTH_COOKIE_SECURE", "true").lower() not in ("0", "false", "no")
    )
    auth_cookie_domain: str = field(default_factory=lambda: _env("AUTH_COOKIE_DOMAIN"))
    # Comma-separated exact origins allowed to call the API with
    # credentials (CORS). Empty => same-origin only (no CORS headers).
    auth_allowed_origins: str = field(default_factory=lambda: _env("AUTH_ALLOWED_ORIGINS"))
    # Failed-login lockout: N attempts per window per (username+ip).
    auth_login_max_attempts: int = field(
        default_factory=lambda: _env_int("AUTH_LOGIN_MAX_ATTEMPTS", 5)
    )
    auth_login_window_seconds: int = field(
        default_factory=lambda: _env_int("AUTH_LOGIN_WINDOW_SECONDS", 300)
    )
    # One-time admin bootstrap: if BOTH are set and the users table is
    # empty at startup, a single admin user is created and logged.
    auth_bootstrap_admin_username: str = field(
        default_factory=lambda: _env("AUTH_BOOTSTRAP_ADMIN_USERNAME")
    )
    auth_bootstrap_admin_password: str = field(
        default_factory=lambda: _env("AUTH_BOOTSTRAP_ADMIN_PASSWORD")
    )

    # --- Stage 19: realtime (WebSocket) monitoring --------------------
    realtime_enabled: bool = field(
        default_factory=lambda: _env("REALTIME_ENABLED", "true").lower() not in ("0", "false", "no")
    )
    realtime_ping_interval_seconds: int = field(
        default_factory=lambda: _env_int("REALTIME_PING_INTERVAL_SECONDS", 25)
    )
    realtime_client_timeout_seconds: int = field(
        default_factory=lambda: _env_int("REALTIME_CLIENT_TIMEOUT_SECONDS", 60)
    )

    # --- Stage 19: market-data engine (ICICI Breeze) ------------------
    # Opt-in: a normal backend deploy does NOT try to reach Breeze unless
    # this is set. The worker + scheduler only start when it is true.
    market_data_enabled: bool = field(default_factory=lambda: _env_bool("MARKET_DATA_ENABLED"))
    market_data_provider: str = field(
        default_factory=lambda: _env("MARKET_DATA_PROVIDER", "icici_breeze").lower()
    )
    market_data_timezone: str = field(
        default_factory=lambda: _env("MARKET_DATA_TIMEZONE", "Asia/Kolkata")
    )
    market_data_start_time: str = field(
        default_factory=lambda: _env("MARKET_DATA_START_TIME", "09:10")
    )
    market_data_stop_time: str = field(
        default_factory=lambda: _env("MARKET_DATA_STOP_TIME", "15:45")
    )
    # Optional exchange-holiday list, "YYYY-MM-DD,YYYY-MM-DD" (Phase 15
    # will replace this with a proper calendar; empty = weekday-only).
    market_data_holidays: str = field(default_factory=lambda: _env("MARKET_DATA_HOLIDAYS"))
    nifty_option_strike_range: int = field(
        default_factory=lambda: _env_int("NIFTY_OPTION_STRIKE_RANGE", 10)
    )
    market_data_stale_seconds: int = field(
        default_factory=lambda: _env_int("MARKET_DATA_STALE_SECONDS", 10)
    )
    market_ws_update_interval_ms: int = field(
        default_factory=lambda: _env_int("MARKET_WS_UPDATE_INTERVAL_MS", 1000)
    )
    market_data_retention_days: int = field(
        default_factory=lambda: _env_int("MARKET_DATA_RETENTION_DAYS", 365)
    )
    option_data_retention_days: int = field(
        default_factory=lambda: _env_int("OPTION_DATA_RETENTION_DAYS", 180)
    )
    # ICICI Breeze credentials. Primary names are BREEZE_*; the older
    # ICICI_BREEZE_* names (BrokerCredentials / the order stub) are
    # accepted as a fallback so an existing env keeps working. Never
    # logged, never returned by any API response.
    breeze_enabled: bool = field(default_factory=lambda: _env_bool("BREEZE_ENABLED"))
    breeze_api_key: str = field(
        default_factory=lambda: _env("BREEZE_API_KEY") or _env("ICICI_BREEZE_API_KEY")
    )
    breeze_secret_key: str = field(
        default_factory=lambda: _env("BREEZE_SECRET_KEY") or _env("ICICI_BREEZE_API_SECRET")
    )
    breeze_session_token: str = field(
        default_factory=lambda: _env("BREEZE_SESSION_TOKEN") or _env("ICICI_BREEZE_SESSION_TOKEN")
    )
    # Optional: an AWS Secrets Manager secret ARN/name holding a JSON blob
    # {"api_key","secret_key","session_token"}. When set it takes
    # precedence over the env values above (read lazily via boto3).
    breeze_secret_id: str = field(default_factory=lambda: _env("BREEZE_SECRET_ID"))

    # --- Stale-heartbeat watcher --------------------------------
    stale_threshold_minutes: float = field(
        default_factory=lambda: _env_float("STALE_THRESHOLD_MINUTES", 2.0)
    )
    stale_check_interval_seconds: int = field(
        default_factory=lambda: _env_int("STALE_CHECK_INTERVAL_SECONDS", 60)
    )
    disable_background_watcher: bool = field(
        default_factory=lambda: _env_bool("DISABLE_BACKGROUND_WATCHER")
    )
    day_loss_limit: float = field(default_factory=lambda: _env_float("DAY_LOSS_LIMIT", 10000.0))

    # --- AWS / Lambda ----------------------------------------
    aws_region: str = field(default_factory=lambda: _env("AWS_REGION"))
    lambda_function_name: str = field(default_factory=lambda: _env("LAMBDA_FUNCTION_NAME"))

    # --- Broker reconnect behavior -------------------------
    broker_reconnect_max_attempts: int = field(
        default_factory=lambda: _env_int("BROKER_RECONNECT_MAX_ATTEMPTS", 5)
    )
    broker_reconnect_backoff_seconds: float = field(
        default_factory=lambda: _env_float("BROKER_RECONNECT_BACKOFF_SECONDS", 2.0)
    )

    # --- Telegram alerts (alerts/telegram.py still reads its own) -----
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    alert_dedup_seconds: int = field(default_factory=lambda: _env_int("ALERT_DEDUP_SECONDS", 120))
    alert_max_retries: int = field(default_factory=lambda: _env_int("ALERT_MAX_RETRIES", 3))
    alert_retry_delay: float = field(default_factory=lambda: _env_float("ALERT_RETRY_DELAY", 2.0))

    # --- Broker credentials (reused from trading.common.config) ------
    credentials: BrokerCredentials = field(default_factory=BrokerCredentials)

    @property
    def is_live(self) -> bool:
        """True only when TRADING_MODE is exactly 'live'. Anything else
        (unset, 'paper', 'test', a typo) is treated as paper -- fail safe."""
        return self.trading_mode == "live"


def load_settings() -> Settings:
    """Read a fresh Settings snapshot from the current environment."""
    return Settings()
