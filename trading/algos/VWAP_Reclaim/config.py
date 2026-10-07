"""
VWAP_Reclaim settings.

Strategy rules are frozen so the paper run matches the backtest. Only
operational knobs (capital, lots, mode, timings) come from env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path

ALGO_NAME = "VWAP_Reclaim"
SIGNAL = "VWAP_Reclaim"

# ─── Strategy rules (fixed; must match the backtest) ─────────────────────────
STRIKE_STEP = 50
STRIKE_OFFSET_ITM = -1                   # 1-ITM: CE strike = ATM-50, PE strike = ATM+50
SL_PCT = 0.30                            # hard SL at -30% premium
TARGET1_PCT = 0.30                       # book 50% at +30%
EMA_TRAIL_PERIOD = 10                    # trail on 5-min spot EMA10 after T1
MAX_LOSSES_PER_DAY = 2
WEEKLY_DD_HALT_PCT = 0.03                # halt entries until Monday after -3% WTD

SCAN_FROM = dtime(9, 30)
SCAN_TO   = dtime(13, 0)
HARD_EXIT_TIME = dtime(14, 30)
SKIP_EXPIRY_DAY = True

# ─── Operational config (env-overridable) ────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _parse_hhmm(raw: str) -> dtime:
    hh, mm = raw.strip().split(":")
    return dtime(int(hh), int(mm))


@dataclass(frozen=True)
class VwapReclaimConfig:
    mode: str = field(default_factory=lambda: os.environ.get("VWAP_RECLAIM_MODE", "paper").strip().lower())
    initial_capital: float = field(
        default_factory=lambda: float(os.environ.get("VWAP_RECLAIM_CAPITAL", "200000"))
    )
    lot_size: int = field(default_factory=lambda: int(os.environ.get("VWAP_RECLAIM_LOT_SIZE", "75")))
    lots_per_trade: int = field(default_factory=lambda: int(os.environ.get("VWAP_RECLAIM_LOTS", "5")))
    brokerage_per_leg: float = field(
        default_factory=lambda: float(os.environ.get("VWAP_RECLAIM_BROKERAGE", "30"))
    )
    cost_turnover_pct: float = field(
        default_factory=lambda: float(os.environ.get("VWAP_RECLAIM_COST_PCT", "0.0007"))
    )
    market_open: dtime = field(
        default_factory=lambda: _parse_hhmm(os.environ.get("VWAP_RECLAIM_MARKET_OPEN", "09:15"))
    )
    market_close: dtime = field(
        default_factory=lambda: _parse_hhmm(os.environ.get("VWAP_RECLAIM_MARKET_CLOSE", "15:30"))
    )
    loop_interval_seconds: float = field(
        default_factory=lambda: float(os.environ.get("VWAP_RECLAIM_LOOP_INTERVAL", "15"))
    )
    heartbeat_push_minutes: int = 1
    state_file: Path = field(default_factory=lambda: DATA_DIR / f"{ALGO_NAME}.portfolio.json")


def load_strategy_config() -> VwapReclaimConfig:
    return VwapReclaimConfig()
