"""
market_guard.py
Refuse to trade when the exchange is closed.

Background: on 2026-10-20 (NSE holiday, Dussehra) the bot ran a normal session
on the previous day's stale candles, "entered" a position and sent real orders.
The broker queued them as after-market orders and they executed at the next
open (09:15), bypassing the Supertrend/pivot checks entirely.
"""

from datetime import datetime, time as dtime, timedelta, timezone

import config
from logger import get_logger

log = get_logger()

IST = timezone(timedelta(hours=5, minutes=30))


def _parse_hhmm(value):
    h, m = value.split(":")
    return dtime(int(h), int(m))


def now_ist():
    return datetime.now(IST).replace(tzinfo=None)


def is_trading_day(day):
    """True if NSE trades on `day`. Uses the repo's verified NSE calendar; if it
    has no data for that year, falls back to Mon-Fri with a loud warning (the
    market-hours and stale-candle guards still apply)."""
    try:
        from trading.ai_research.market.calendar_nse import (
            CalendarCoverageError, is_trading_day as _nse_is_trading_day,
        )
    except ImportError:  # repo root not on sys.path
        log.warning("NSE calendar unavailable; assuming Mon-Fri trading days.")
        return day.weekday() < 5
    try:
        return _nse_is_trading_day(day)
    except CalendarCoverageError as exc:
        log.warning("%s -- falling back to Mon-Fri.", exc)
        return day.weekday() < 5


def market_is_open(now=None):
    """True only on a trading day between MARKET_OPEN and MARKET_CLOSE (IST)."""
    now = now or now_ist()
    if not is_trading_day(now.date()):
        return False
    return _parse_hhmm(config.MARKET_OPEN) <= now.time() <= _parse_hhmm(config.MARKET_CLOSE)
