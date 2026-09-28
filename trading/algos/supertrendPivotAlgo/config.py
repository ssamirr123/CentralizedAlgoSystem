"""
config.py
Central configuration for the Angel One option-selling bot.

IMPORTANT: Do not commit real credentials. Prefer environment variables.
"""

import os


# ----------------------------------------------------------------------------
# Angel One SmartAPI credentials
# ----------------------------------------------------------------------------
# Read from the environment, falling back to the git-ignored trading/.env at
# the repo root -- same variable names as the other algos on the box.
# NEVER put real values in this tracked file.
def _read_env_file():
    from pathlib import Path as _P
    _envf = _P(__file__).resolve().parents[3] / "trading" / ".env"
    if _envf.is_file():
        for _raw in _envf.read_text(encoding="utf-8", errors="ignore").splitlines():
            _raw = _raw.strip()
            if _raw and not _raw.startswith("#") and "=" in _raw:
                _k, _, _v = _raw.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))


_read_env_file()

def _angel_creds() -> dict:
    """AngelOne credentials from the environment (same names as the other algos)."""
    def _g(*names):
        for _n in names:
            _val = os.environ.get(_n, "").strip()
            if _val:
                return _val
        return ""

    return {
        "clientid": _g("ANGELONE_CLIENT_ID"),
        "apikey": _g("ANGELONE_API_KEY"),
        "mpin": _g("ANGELONE_MPIN", "ANGELONE_PASSWORD"),
        "token": _g("ANGELONE_TOTP_SECRET"),
    }


_ANGEL = _angel_creds()
clientid = _ANGEL["clientid"]
apikey = _ANGEL["apikey"]
mpin = _ANGEL["mpin"]
token = _ANGEL["token"]

# ----------------------------------------------------------------------------
# Instrument / market settings
# ----------------------------------------------------------------------------
# Which index to trade: "NIFTY" or "BANKNIFTY"
INDEX = os.getenv("BOT_INDEX", "NIFTY")

# Per-index metadata.
INDEX_CONFIG = {
    "NIFTY": {
        "name": "NIFTY",
        "spot_symbol": "NIFTY",
        "spot_token": "99926000",      # NSE index token for spot
        "exchange": "NFO",             # options exchange
        "spot_exchange": "NSE",
        "feed_exchange_type": 1,       # SmartWebSocketV2 exchangeType: 1 = nse_cm
        "strike_step": 50,             # strike spacing
        "lot_size": 65,                # update to current NSE lot size
    },
    "BANKNIFTY": {
        "name": "BANKNIFTY",
        "spot_symbol": "BANKNIFTY",
        "spot_token": "99926009",
        "exchange": "NFO",
        "spot_exchange": "NSE",
        "feed_exchange_type": 1,       # SmartWebSocketV2 exchangeType: 1 = nse_cm
        "strike_step": 100,
        "lot_size": 30,                # update to current NSE lot size
    },
}


# ----------------------------------------------------------------------------
# Candle / timeframe settings
# ----------------------------------------------------------------------------
CANDLE_INTERVAL = "FIVE_MINUTE"        # SmartAPI interval string
CANDLE_INTERVAL_MINUTES = 5            # minutes per candle (must match above)

# ----------------------------------------------------------------------------
# Historical (REST) candle fetch resilience
# ----------------------------------------------------------------------------
# Angel One's historical-data endpoint is aggressively rate limited. When a
# live WebSocket stream is attached, the multi-day REST history only supplies
# the warm-up bars (pivots + Supertrend) which do not change intraday, so it is
# cached and refreshed at most every REST_CANDLE_CACHE_SECONDS. Without a stream
# the cache is bypassed so the strategy always sees the latest closed candle.
REST_CANDLE_CACHE_SECONDS = int(os.getenv("BOT_REST_CANDLE_CACHE_SECONDS", "1800"))
# Retry a rate-limited / failed candle request this many times before giving up.
CANDLE_FETCH_MAX_RETRIES = int(os.getenv("BOT_CANDLE_FETCH_MAX_RETRIES", "4"))
# Base back-off (seconds) between candle fetch retries; grows linearly per try.
CANDLE_FETCH_BACKOFF_SECONDS = float(os.getenv("BOT_CANDLE_FETCH_BACKOFF_SECONDS", "2"))
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"
FIRST_CANDLE_CLOSE = "09:20"           # first 5-min candle completes
LAST_ENTRY_TIME = "15:00"              # no new entries after this
SQUARE_OFF_TIME = "15:15"             # force exit everything


# ----------------------------------------------------------------------------
# Indicator settings
# ----------------------------------------------------------------------------
SUPERTREND_PERIOD = 7
SUPERTREND_MULTIPLIER = 3


# ----------------------------------------------------------------------------
# Order / risk settings
# ----------------------------------------------------------------------------
# "NRML" (carry/normal) or "MIS" (intraday)
PRODUCT_TYPE = os.getenv("BOT_PRODUCT_TYPE", "INTRADAY")  # SmartAPI: INTRADAY / CARRYFORWARD
ORDER_VARIETY = "NORMAL"
ORDER_TYPE = "MARKET"
QUANTITY_LOTS = int(os.getenv("BOT_QUANTITY_LOTS", "1"))

# Risk controls
MAX_TRADES_PER_DAY = int(os.getenv("BOT_MAX_TRADES_PER_DAY", "3"))
# Allow only one open option position at a time.
ALLOW_SIMULTANEOUS_POSITIONS = False


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------
LOG_DIR = os.getenv("BOT_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
LOG_LEVEL = os.getenv("BOT_LOG_LEVEL", "INFO")


# Accept both BOT_DRY_RUN and BOT_DY_RUN to avoid env var naming mistakes.
def _env_flag(name, default="false"):
    value = os.getenv(name, default)
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


# Set True to run without placing real orders (paper trading).
DRY_RUN = _env_flag("BOT_DRY_RUN", os.getenv("BOT_DY_RUN", "true"))


# ----------------------------------------------------------------------------
# Live market feed (WebSocket)
# ----------------------------------------------------------------------------
# When True, stream live ticks over SmartWebSocketV2 and build the current
# 5-minute candle from ticks instead of relying only on delayed REST candles.
USE_WEBSOCKET = _env_flag("BOT_USE_WEBSOCKET", "true")


# ----------------------------------------------------------------------------
# Central monitoring (control-center dashboard)
# ----------------------------------------------------------------------------
# Folder name == the algo id registered on the dashboard; used for the PID /
# stop-flag files that the Start/Stop commands rely on.
ALGO_NAME = "supertrendPivotAlgo"

# trading_agent.py (the dashboard's Start command) exports these four when it
# launches the strategy. If any is missing (e.g. a manual run) heartbeats are
# skipped and the strategy still trades normally.
MONITOR_ENABLED = _env_flag("MONITOR_ENABLED", "true")
STRATEGY_NAME = os.getenv("STRATEGY_NAME", ALGO_NAME)
SERVER_NAME = os.getenv("SERVER_NAME", "")
API_BASE_URL = os.getenv("API_BASE_URL", "")
CONTROL_API_KEY = os.getenv("CONTROL_API_KEY", "")
MONITOR_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("MONITOR_HEARTBEAT_INTERVAL_SECONDS", "10"))
# How often open-position MTM is refreshed between trades (one ltpData call).
MONITOR_REFRESH_SECONDS = int(os.getenv("MONITOR_REFRESH_SECONDS", "30"))


def get_index_config():
    """Return the configuration dict for the currently selected index."""
    if INDEX not in INDEX_CONFIG:
        raise ValueError(f"Unsupported INDEX '{INDEX}'. Use one of {list(INDEX_CONFIG)}")
    return INDEX_CONFIG[INDEX]
