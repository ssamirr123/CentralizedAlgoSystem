"""
Central configuration for VWAP_Reclaim.

Mirrors the supertrendPivotAlgo layout: strategy rules are module-level
constants (so a `from config import SL_PCT` matches imports elsewhere),
operational knobs come from env vars (with sensible defaults), and the
same dashboard-related vars (SERVER_NAME / API_BASE_URL / CONTROL_API_KEY
/ STRATEGY_NAME) are read directly here so `monitor.py` can decide
whether to attach.

IMPORTANT: Never commit real credentials. Prefer env vars or the
git-ignored `trading/.env` sibling of the repo root.
"""
from __future__ import annotations

import os
from datetime import date, time as dtime
from pathlib import Path


# ─── Env loader ──────────────────────────────────────────────────────────────
def _read_env_file() -> None:
    """Load `trading/.env` into os.environ (same convention as the other algos
    on this host). Never overrides a value already present."""
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
    """Wrapper so monitor/scheduler share one 'today' definition if the host
    clock drifts across midnight mid-tick."""
    return date.today()


# ─── Algo identity ───────────────────────────────────────────────────────────
# Folder name == dashboard algo id (PID / stop-flag files key off this).
ALGO_NAME = "VWAP_Reclaim"
STRATEGY_NAME = os.getenv("STRATEGY_NAME", ALGO_NAME)

# ─── Instrument / market settings ────────────────────────────────────────────
INDEX = os.getenv("VWAP_RECLAIM_INDEX", "NIFTY")
STRIKE_STEP = 50
LOT_SIZE = int(os.getenv("VWAP_RECLAIM_LOT_SIZE", "75"))
QUANTITY_LOTS = int(os.getenv("VWAP_RECLAIM_LOTS", "5"))

# ─── Strategy rules (fixed; must match the backtest) ─────────────────────────
STRIKE_OFFSET_ITM = -1                   # 1-ITM
SL_PCT = 0.30                            # hard SL at -30% premium
TARGET1_PCT = 0.30                       # book 50% at +30%
EMA_TRAIL_PERIOD = 10                    # 5-min spot EMA10 trail after T1
MAX_LOSSES_PER_DAY = 2
WEEKLY_DD_HALT_PCT = 0.03                # halt until Monday after -3% WTD
SKIP_EXPIRY_DAY = _env_flag("VWAP_RECLAIM_SKIP_EXPIRY_DAY", "true")

# Session + scan windows
MARKET_OPEN = os.getenv("VWAP_RECLAIM_MARKET_OPEN", "09:15")
MARKET_CLOSE = os.getenv("VWAP_RECLAIM_MARKET_CLOSE", "15:30")
SCAN_FROM = _parse_hhmm(os.getenv("VWAP_RECLAIM_SCAN_FROM", "09:30"))
SCAN_TO = _parse_hhmm(os.getenv("VWAP_RECLAIM_SCAN_TO", "13:00"))
HARD_EXIT_TIME = _parse_hhmm(os.getenv("VWAP_RECLAIM_HARD_EXIT", "14:30"))

# ─── Capital + costs ─────────────────────────────────────────────────────────
INITIAL_CAPITAL = float(os.getenv("VWAP_RECLAIM_CAPITAL", "200000"))
BROKERAGE_PER_LEG = float(os.getenv("VWAP_RECLAIM_BROKERAGE", "30"))
COST_TURNOVER_PCT = float(os.getenv("VWAP_RECLAIM_COST_PCT", "0.0007"))

# ─── Runtime ─────────────────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS = float(os.getenv("VWAP_RECLAIM_LOOP_INTERVAL", "15"))
# True = paper fills from the broker-quote adapter; a live broker leg is only
# attempted when this is False (not implemented yet).
DRY_RUN = _env_flag("VWAP_RECLAIM_DRY_RUN", "true")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
STATE_FILE = DATA_DIR / f"{ALGO_NAME}.portfolio.json"

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_DIR = os.getenv("VWAP_RECLAIM_LOG_DIR",
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
LOG_LEVEL = os.getenv("VWAP_RECLAIM_LOG_LEVEL", "INFO")

# ─── Dashboard integration (exported by the Start command) ───────────────────
MONITOR_ENABLED = _env_flag("MONITOR_ENABLED", "true")
SERVER_NAME = os.getenv("SERVER_NAME", "")
API_BASE_URL = os.getenv("API_BASE_URL", "")
CONTROL_API_KEY = os.getenv("CONTROL_API_KEY", "")
MONITOR_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("MONITOR_HEARTBEAT_INTERVAL_SECONDS", "10"))
MONITOR_REFRESH_SECONDS = int(os.getenv("MONITOR_REFRESH_SECONDS", "30"))
