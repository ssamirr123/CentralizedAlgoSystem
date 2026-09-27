"""
ETF_Momentum settings.

The strategy rules below are identical to the validated backtest
(ETF_Momentum_Test_1.py) and deliberately NOT env-configurable -- changing
any of them means the live run no longer matches the backtest it was
approved on. Only operational knobs (capital, timing, mode) come from env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path

ALGO_NAME = "ETF_Momentum"

# ─── Strategy rules (fixed; must match the backtest) ─────────────────────────
N_HOLD = 6
LB = (21, 63, 126, 252)                  # 1M, 3M, 6M, 12M in trading days
WEIGHTS = (0.15, 0.40, 0.30, 0.15)
DMA_PERIOD = 200
MIN_HISTORY = 262                        # 252 + 10 buffer
SL_PCT = 0.15                            # sell if price <= entry * 0.85
TRAIL_PCT = 0.20                         # sell if price <= peak * 0.80
LIQUID_RATE = 0.05                       # p.a. on idle cash

# ─── C54 universe (NSE symbol, category) ─────────────────────────────────────
UNIVERSE: tuple[tuple[str, str], ...] = (
    ("NIFTYBEES", "Broad_Equity"), ("JUNIORBEES", "Broad_Equity"), ("MID150BEES", "Broad_Equity"),
    ("MIDSELIETF", "Broad_Equity"), ("MONIFTY500", "Broad_Equity"), ("HDFCSML250", "Broad_Equity"),
    ("MOM100", "Broad_Equity"),
    ("BANKBEES", "Banking_Finance"), ("PSUBNKBEES", "Banking_Finance"),
    ("PVTBANIETF", "Banking_Finance"), ("FINIETF", "Banking_Finance"),
    ("ITBEES", "Sector"), ("PHARMABEES", "Sector"), ("AUTOBEES", "Sector"), ("CONSUMBEES", "Sector"),
    ("CONSUMER", "Sector"), ("FMCGIETF", "Sector"), ("CHEMICAL", "Sector"), ("METALIETF", "Sector"),
    ("OILIETF", "Sector"), ("COMMOIETF", "Sector"), ("INFRAIETF", "Sector"), ("CPSEETF", "Sector"),
    ("ICICIB22", "Sector"), ("MOCAPITAL", "Sector"), ("MODEFENCE", "Sector"), ("MOREALTY", "Sector"),
    ("MOTOUR", "Sector"), ("GROWWPOWER", "Sector"), ("GROWWRAIL", "Sector"), ("GROWWHOSPI", "Sector"),
    ("DIVOPPBEES", "Thematic"), ("EVINDIA", "Thematic"), ("INTERNET", "Thematic"), ("MNC", "Thematic"),
    ("SELECTIPO", "Thematic"), ("TOP10ADD", "Thematic"),
    ("LOWVOLIETF", "Factor"), ("NV20IETF", "Factor"), ("QUAL30IETF", "Factor"), ("NIFTYQLITY", "Factor"),
    ("MONQ50", "Factor"), ("HDFCGROWTH", "Factor"), ("MOM50", "Factor"), ("MOMENTUM50", "Factor"),
    ("MOVALUE", "Factor"),
    ("MON100", "International"), ("MAFANG", "International"), ("HNGSNGBEES", "International"),
    ("GOLDBEES", "Gold_Silver"), ("SILVERBEES", "Gold_Silver"),
    ("LTGILTBEES", "Bonds"), ("EBBETF0430", "Bonds"), ("GILT5YBEES", "Bonds"),
)
CATEGORY = dict(UNIVERSE)

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _parse_hhmm(raw: str) -> dtime:
    hh, mm = raw.strip().split(":")
    return dtime(int(hh), int(mm))


@dataclass(frozen=True)
class EtfMomentumConfig:
    # Only "paper" is implemented: fills are simulated at the rebalance-day close.
    mode: str = field(default_factory=lambda: os.environ.get("ETF_MOMENTUM_MODE", "paper").strip().lower())
    initial_capital: float = field(
        default_factory=lambda: float(os.environ.get("ETF_MOMENTUM_CAPITAL", "1000000"))
    )
    # IST. After the 15:30 close so the day's close is final in the data feed.
    rebalance_time: dtime = field(
        default_factory=lambda: _parse_hhmm(os.environ.get("ETF_MOMENTUM_REBALANCE_TIME", "16:00"))
    )
    # true -> a fresh portfolio rebalances on the next trading day instead of
    # waiting for the first trading day of next month.
    start_now: bool = field(
        default_factory=lambda: os.environ.get("ETF_MOMENTUM_START_NOW", "false").strip().lower()
        in ("1", "true", "yes", "on")
    )
    history_calendar_days: int = 450     # ~300 trading rows: covers MIN_HISTORY + DMA
    loop_interval_seconds: float = 30.0
    price_refresh_minutes: int = 15
    rebalance_retry_minutes: int = 15
    rebalance_max_attempts_per_day: int = 8
    state_file: Path = field(default_factory=lambda: DATA_DIR / f"{ALGO_NAME}.portfolio.json")


def load_strategy_config() -> EtfMomentumConfig:
    return EtfMomentumConfig()
