"""
Pure signal + exit logic for VWAP_Reclaim.

No I/O. The runner feeds in minute-level NIFTY spot and current timestamp.
Returns SignalDecision (entry) or ExitDecision (exit).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pandas as pd

from trading.algos.VWAP_Reclaim.config import (
    EMA_TRAIL_PERIOD, HARD_EXIT_TIME, SCAN_FROM, SCAN_TO,
    STRIKE_OFFSET_ITM, STRIKE_STEP,
)


@dataclass(frozen=True)
class SignalDecision:
    direction: str
    strike: int
    spot: float
    entry_ts: datetime


@dataclass(frozen=True)
class ExitDecision:
    reason: str                 # "SL" | "Trail" | "TimeStop"
    price: float
    at_ts: datetime


def _round_strike(spot_price: float, direction: str,
                  offset: int = STRIKE_OFFSET_ITM, step: int = STRIKE_STEP) -> int:
    atm = round(spot_price / step) * step
    if direction == "CE":
        return atm + offset * step
    return atm - offset * step


def _resample_5min(spot_minutes: pd.DataFrame) -> pd.DataFrame:
    o = spot_minutes["Open"].resample("5min").first()
    h = spot_minutes["High"].resample("5min").max()
    low = spot_minutes["Low"].resample("5min").min()
    c = spot_minutes["Close"].resample("5min").last()
    return pd.DataFrame({"Open": o, "High": h, "Low": low, "Close": c}).dropna()


def _vwap(spot_minutes: pd.DataFrame) -> pd.Series:
    tp = (spot_minutes["High"] + spot_minutes["Low"] + spot_minutes["Close"]) / 3
    return tp.expanding().mean()


def detect(spot_minutes: pd.DataFrame, now: datetime) -> Optional[SignalDecision]:
    """VWAP Reclaim: price dips below cumulative VWAP, then a 5-min bar
    opens below VWAP and closes above → CE. Mirror → PE."""
    if spot_minutes.empty:
        return None
    bars5 = _resample_5min(spot_minutes)
    scan = bars5.between_time(SCAN_FROM, SCAN_TO)
    if scan.empty:
        return None
    vwap_min = _vwap(spot_minutes)

    was_below = False
    was_above = False
    for ts, bar in scan.iterrows():
        if ts > now:
            break
        v = vwap_min.asof(ts)
        if v is None or pd.isna(v):
            continue
        if bar["Close"] < v:
            was_below = True
        if bar["Close"] > v:
            was_above = True
        if was_below and bar["Open"] < v and bar["Close"] > v:
            spot_px = float(bar["Close"])
            return SignalDecision("CE", _round_strike(spot_px, "CE"), spot_px, ts.to_pydatetime())
        if was_above and bar["Open"] > v and bar["Close"] < v:
            spot_px = float(bar["Close"])
            return SignalDecision("PE", _round_strike(spot_px, "PE"), spot_px, ts.to_pydatetime())
    return None


def evaluate_exit(leg, option_ltp: float, spot_minutes: pd.DataFrame, now: datetime) -> Optional[ExitDecision]:
    if now.time() >= HARD_EXIT_TIME:
        return ExitDecision("TimeStop", option_ltp, now)
    if option_ltp <= leg.sl_price:
        return ExitDecision("SL", leg.sl_price, now)
    if leg.booked_half and not spot_minutes.empty:
        bars5 = _resample_5min(spot_minutes)
        if len(bars5) >= EMA_TRAIL_PERIOD:
            ema = bars5["Close"].ewm(span=EMA_TRAIL_PERIOD, adjust=False).mean()
            last_close = float(bars5["Close"].iloc[-1])
            last_ema = float(ema.iloc[-1])
            if leg.direction == "CE" and last_close < last_ema:
                return ExitDecision("Trail", option_ltp, now)
            if leg.direction == "PE" and last_close > last_ema:
                return ExitDecision("Trail", option_ltp, now)
    return None


def is_tradable_day(d: date, weekly_expiries: list[date], skip_expiry: bool) -> bool:
    if d.weekday() >= 5:
        return False
    if skip_expiry and d in weekly_expiries:
        return False
    return True
