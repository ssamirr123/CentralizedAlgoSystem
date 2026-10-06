"""
Pure signal + exit logic for EMA20_Pullback.

No I/O. The runner feeds in minute-level NIFTY spot and current timestamp.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pandas as pd

from trading.algos.EMA20_Pullback.config import (
    EMA_FAST, EMA_SLOW, EMA_TRAIL_PERIOD, HARD_EXIT_TIME, SCAN_FROM, SCAN_TO,
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
    reason: str
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


def detect(spot_minutes: pd.DataFrame, now: datetime) -> Optional[SignalDecision]:
    """EMA20 Pullback: EMA20>EMA50 trend regime, prior bar low touches
    EMA20, current bar closes above prior high → CE. Mirror → PE."""
    if spot_minutes.empty:
        return None
    bars5 = _resample_5min(spot_minutes)
    if len(bars5) < EMA_SLOW:
        return None
    bars5 = bars5.copy()
    bars5["ema_fast"] = bars5["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    bars5["ema_slow"] = bars5["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    scan = bars5.between_time(SCAN_FROM, SCAN_TO)
    if len(scan) < 2:
        return None

    for i in range(1, len(scan)):
        ts = scan.index[i]
        if ts > now:
            break
        prev = scan.iloc[i - 1]
        cur = scan.iloc[i]
        # Uptrend regime: EMA20 > EMA50; prior-bar low touches EMA20 (within 0.1%);
        # current bar closes above prior high → long CE.
        if cur["ema_fast"] > cur["ema_slow"] and prev["Low"] <= prev["ema_fast"] * 1.001 \
                and cur["Close"] > prev["High"]:
            spot_px = float(cur["Close"])
            return SignalDecision("CE", _round_strike(spot_px, "CE"), spot_px, ts.to_pydatetime())
        # Mirror for downtrend → long PE.
        if cur["ema_fast"] < cur["ema_slow"] and prev["High"] >= prev["ema_fast"] * 0.999 \
                and cur["Close"] < prev["Low"]:
            spot_px = float(cur["Close"])
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
