"""
Central configuration for EMA20_Pullback.

Mirrors the supertrendPivotAlgo layout: strategy rules are module-level
constants, operational knobs come from env vars, and the dashboard-
related vars (SERVER_NAME / API_BASE_URL / CONTROL_API_KEY / STRATEGY_NAME)
are read here so `monitor.py` can decide whether to attach.

IMPORTANT: Never commit real credentials. Prefer env vars or the
git-ignored `trading/.env`.
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
    value = os.getenv(name, default)
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _parse_hhmm(raw: str) -> dtime:
    h, m = raw.strip().split(":")
    return dtime(int(h), int(m))


def today() -> date:
    return date.today()


# ─── Algo identity ───────────────────────────────────────────────────────────
ALGO_NAME = "EMA20_Pullback"
STRATEGY_NAME = os.getenv("STRATEGY_NAME", ALGO_NAME)

# ─── Instrument / market settings ────────────────────────────────────────────
INDEX = os.getenv("EMA20_PULLBACK_INDEX", "NIFTY")
STRIKE_STEP = 50
LOT_SIZE = int(os.getenv("EMA20_PULLBACK_LOT_SIZE", "75"))
QUANTITY_LOTS = int(os.getenv("EMA20_PULLBACK_LOTS", "5"))

# ─── Strategy rules (fixed; must match the backtest) ─────────────────────────
STRIKE_OFFSET_ITM = -1
SL_PCT = 0.30
TARGET1_PCT = 0.40                       # EMA20 trend trades have more follow-through
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

# ─── Capital + costs ─────────────────────────────────────────────────────────
INITIAL_CAPITAL = float(os.getenv("EMA20_PULLBACK_CAPITAL", "200000"))
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

# ─── Dashboard integration (exported by the Start command) ───────────────────
MONITOR_ENABLED = _env_flag("MONITOR_ENABLED", "true")
SERVER_NAME = os.getenv("SERVER_NAME", "")
API_BASE_URL = os.getenv("API_BASE_URL", "")
CONTROL_API_KEY = os.getenv("CONTROL_API_KEY", "")
MONITOR_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("MONITOR_HEARTBEAT_INTERVAL_SECONDS", "10"))
MONITOR_REFRESH_SECONDS = int(os.getenv("MONITOR_REFRESH_SECONDS", "30"))
