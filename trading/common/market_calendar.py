"""
Shared NSE trading-day check for every algo.

On a holiday/weekend Angel One (and other brokers) still accept orders and
silently queue them as AMO, which then fire at the next open (the
2026-10-20 Dussehra incident). Algos must therefore refuse to run/order on
non-trading days. Uses the repo's verified NSE calendar; for years it does
not cover it falls back to Mon-Fri with a loud warning.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))


def today_ist() -> date:
    return datetime.now(_IST).date()


def is_trading_day(day: date | None = None) -> bool:
    day = day or today_ist()
    try:
        from trading.ai_research.market.calendar_nse import (
            CalendarCoverageError, is_trading_day as _nse,
        )
        try:
            return _nse(day)
        except CalendarCoverageError as exc:
            logger.warning("%s -- falling back to Mon-Fri.", exc)
    except ImportError:
        logger.warning("NSE calendar unavailable -- falling back to Mon-Fri.")
    return day.weekday() < 5


def non_trading_reason(day: date | None = None) -> str:
    """Human-readable reason, or '' if `day` is a trading day."""
    day = day or today_ist()
    if is_trading_day(day):
        return ""
    if day.weekday() >= 5:
        return f"weekend ({day.strftime('%A')})"
    return f"NSE holiday ({day.isoformat()})"
