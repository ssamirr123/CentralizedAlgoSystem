"""
EMA20_Pullback settings.

Strategy rules are frozen so the paper run matches the backtest. Only
operational knobs (capital, lots, mode, timings) come from env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path

ALGO_NAME = "EMA20_Pullback"
SIGNAL = "EMA20_Pullback"

# ─── Strategy rules (fixed; must match the backtest) ─────────────────────────
STRIKE_STEP = 50
STRIKE_OFFSET_ITM = -1
SL_PCT = 0.30
TARGET1_PCT = 0.40                       # EMA20 trend trades have more follow-through
EMA_TRAIL_PERIOD = 10
EMA_FAST = 20
EMA_SLOW = 50
MAX_LOSSES_PER_DAY = 2
WEEKLY_DD_HALT_PCT = 0.03

SCAN_FROM = dtime(9, 45)
SCAN_TO   = dtime(13, 30)
HARD_EXIT_TIME = dtime(14, 30)
SKIP_EXPIRY_DAY = True

# ─── Operational config (env-overridable) ────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _parse_hhmm(raw: str) -> dtime:
    hh, mm = raw.strip().split(":")
    return dtime(int(hh), int(mm))


@dataclass(frozen=True)
class EmaPullbackConfig:
    mode: str = field(default_factory=lambda: os.environ.get("EMA20_PULLBACK_MODE", "paper").strip().lower())
    initial_capital: float = field(
        default_factory=lambda: float(os.environ.get("EMA20_PULLBACK_CAPITAL", "200000"))
    )
    lot_size: int = field(default_factory=lambda: int(os.environ.get("EMA20_PULLBACK_LOT_SIZE", "75")))
    lots_per_trade: int = field(default_factory=lambda: int(os.environ.get("EMA20_PULLBACK_LOTS", "5")))
    brokerage_per_leg: float = field(
        default_factory=lambda: float(os.environ.get("EMA20_PULLBACK_BROKERAGE", "30"))
    )
    cost_turnover_pct: float = field(
        default_factory=lambda: float(os.environ.get("EMA20_PULLBACK_COST_PCT", "0.0007"))
    )
    market_open: dtime = field(
        default_factory=lambda: _parse_hhmm(os.environ.get("EMA20_PULLBACK_MARKET_OPEN", "09:15"))
    )
    market_close: dtime = field(
        default_factory=lambda: _parse_hhmm(os.environ.get("EMA20_PULLBACK_MARKET_CLOSE", "15:30"))
    )
    loop_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("EMA20_PULLBACK_LOOP_INTERVAL", "15"))
    )
    heartbeat_push_minutes: int = 1
    state_file: Path = field(default_factory=lambda: DATA_DIR / f"{ALGO_NAME}.portfolio.json")


def load_strategy_config() -> EmaPullbackConfig:
    return EmaPullbackConfig()
