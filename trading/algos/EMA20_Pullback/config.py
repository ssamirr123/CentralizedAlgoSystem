"""
Central configuration for EMA20_Pullback.

Angel One SmartAPI credentials + index metadata + strategy rules +
dashboard integration flags, keyed off env vars with a trading/.env
fallback. Mirrors supertrendPivotAlgo so one operator runbook covers both.

IMPORTANT: Never commit real credentials.
"""
from __future__ import annotations

import os
from datetime import date, time as dtime
from pathlib import Path


def _read_env_file() -> None:
    _envf = Path(__file__).resolve().parents[3] / "trading" / ".env"
    if _envf.is_file():
        for raw in _envf.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = raw.strip()
            if raw and not raw.startswith("#") and "=" in raw:
                k, _, v = raw.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_read_env_file()


def _env_flag(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "y", "on")


def _parse_hhmm(raw: str) -> dtime:
    h, m = raw.strip().split(":")
    return dtime(int(h), int(m))


def today() -> date:
    return date.today()


# ─── Broker selection ────────────────────────────────────────────────────────
BROKER = os.getenv("EMA20_PULLBACK_BROKER", os.getenv("BROKER", "ANGELONE")).strip().upper()


def _creds(*names) -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


# AngelOne SmartAPI credentials.
clientid = _creds("ANGELONE_CLIENT_ID")
apikey = _creds("ANGELONE_API_KEY")
mpin = _creds("ANGELONE_MPIN", "ANGELONE_PASSWORD")
token = _creds("ANGELONE_TOTP_SECRET")

# Dhan credentials (long-lived access token).
dhan_client_id = _creds("DHAN_CLIENT_ID")
dhan_access_token = _creds("DHAN_ACCESS_TOKEN")

# ─── Algo identity ───────────────────────────────────────────────────────────
ALGO_NAME = "EMA20_Pullback"
STRATEGY_NAME = os.getenv("STRATEGY_NAME", ALGO_NAME)

# ─── Instrument / index metadata ─────────────────────────────────────────────
INDEX = os.getenv("EMA20_PULLBACK_INDEX", "NIFTY")

INDEX_CONFIG = {
    "NIFTY": {
        "name": "NIFTY",
        "spot_symbol": "NIFTY",
        # AngelOne
        "spot_token": "99926000",
        "exchange": "NFO",
        "spot_exchange": "NSE",
        "feed_exchange_type": 1,
        # Dhan equivalents (NIFTY 50 spot: security_id 13 on IDX_I / INDEX)
        "dhan_spot_security_id": int(os.getenv("DHAN_NIFTY_SPOT_SECURITY_ID", "13")),
        "dhan_spot_exchange_segment": os.getenv("DHAN_NIFTY_SPOT_EXCHANGE_SEGMENT", "IDX_I"),
        "dhan_spot_instrument_type": os.getenv("DHAN_NIFTY_SPOT_INSTRUMENT_TYPE", "INDEX"),
        # Common
        "strike_step": 50,
        "lot_size": int(os.getenv("EMA20_PULLBACK_LOT_SIZE", "75")),
    },
}


def get_index_config() -> dict:
    if INDEX not in INDEX_CONFIG:
        raise ValueError(f"Unsupported INDEX '{INDEX}'. Use one of {list(INDEX_CONFIG)}")
    return INDEX_CONFIG[INDEX]


CANDLE_INTERVAL = os.getenv("EMA20_PULLBACK_CANDLE_INTERVAL", "ONE_MINUTE")

REST_CANDLE_CACHE_SECONDS = int(os.getenv("EMA20_PULLBACK_REST_CACHE_SECONDS", "45"))
CANDLE_FETCH_MAX_RETRIES = int(os.getenv("EMA20_PULLBACK_CANDLE_RETRIES", "4"))
CANDLE_FETCH_BACKOFF_SECONDS = float(os.getenv("EMA20_PULLBACK_CANDLE_BACKOFF_SECONDS", "2"))

# ─── Strategy rules (fixed; must match the backtest) ─────────────────────────
STRIKE_STEP = 50
STRIKE_OFFSET_ITM = -1
SL_PCT = 0.30
TARGET1_PCT = 0.40
EMA_TRAIL_PERIOD = 10
EMA_FAST = 20
EMA_SLOW = 50
MAX_LOSSES_PER_DAY = 2
WEEKLY_DD_HALT_PCT = 0.03
SKIP_EXPIRY_DAY = _env_flag("EMA20_PULLBACK_SKIP_EXPIRY_DAY", "true")

MARKET_OPEN = os.getenv("EMA20_PULLBACK_MARKET_OPEN", "09:15")
MARKET_CLOSE = os.getenv("EMA20_PULLBACK_MARKET_CLOSE", "15:30")
SCAN_FROM = _parse_hhmm(os.getenv("EMA20_PULLBACK_SCAN_FROM", "09:45"))
SCAN_TO = _parse_hhmm(os.getenv("EMA20_PULLBACK_SCAN_TO", "13:30"))
HARD_EXIT_TIME = _parse_hhmm(os.getenv("EMA20_PULLBACK_HARD_EXIT", "14:30"))

# ─── Capital + sizing + costs ────────────────────────────────────────────────
INITIAL_CAPITAL = float(os.getenv("EMA20_PULLBACK_CAPITAL", "200000"))
LOT_SIZE = int(os.getenv("EMA20_PULLBACK_LOT_SIZE", "75"))
QUANTITY_LOTS = int(os.getenv("EMA20_PULLBACK_LOTS", "5"))
BROKERAGE_PER_LEG = float(os.getenv("EMA20_PULLBACK_BROKERAGE", "30"))
COST_TURNOVER_PCT = float(os.getenv("EMA20_PULLBACK_COST_PCT", "0.0007"))

# ─── Runtime ─────────────────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS = float(os.getenv("EMA20_PULLBACK_LOOP_INTERVAL", "15"))
DRY_RUN = _env_flag("EMA20_PULLBACK_DRY_RUN", "true")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
STATE_FILE = DATA_DIR / f"{ALGO_NAME}.portfolio.json"

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_DIR = os.getenv("EMA20_PULLBACK_LOG_DIR",
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
LOG_LEVEL = os.getenv("EMA20_PULLBACK_LOG_LEVEL", "INFO")

# ─── Dashboard integration ───────────────────────────────────────────────────
MONITOR_ENABLED = _env_flag("MONITOR_ENABLED", "true")
SERVER_NAME = os.getenv("SERVER_NAME", "")
API_BASE_URL = os.getenv("API_BASE_URL", "")
CONTROL_API_KEY = os.getenv("CONTROL_API_KEY", "")
MONITOR_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("MONITOR_HEARTBEAT_INTERVAL_SECONDS", "10"))
MONITOR_REFRESH_SECONDS = int(os.getenv("MONITOR_REFRESH_SECONDS", "30"))
