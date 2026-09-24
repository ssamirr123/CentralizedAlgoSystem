"""
NSE trading calendar tests -- exercised against the REAL verified 2025/
2026 holiday data embedded in calendar_nse.py (fetched live from NSE's
own holiday-master API during Phase 7 development), not synthetic dates.
"""
from __future__ import annotations

from datetime import date

import pytest

from trading.ai_research.market.calendar_nse import (
    IST,
    CalendarCoverageError,
    is_trading_day,
    market_close,
    market_open,
    next_trading_day,
    previous_trading_day,
    trading_days_in_range,
    validate_research_date,
)


def test_known_real_holiday_is_not_a_trading_day():
    """26 Jan 2026 (Republic Day, Monday) -- real NSE holiday."""
    assert is_trading_day(date(2026, 1, 26)) is False


def test_known_real_trading_day():
    """22 Sep 2026 is a Tuesday and not in the holiday list."""
    assert is_trading_day(date(2026, 9, 22)) is True


def test_weekend_is_not_a_trading_day():
    # 2026-09-20 is a Sunday.
    assert is_trading_day(date(2026, 9, 20)) is False
    assert is_trading_day(date(2026, 9, 19)) is False  # Saturday


def test_weekday_that_is_not_a_holiday_is_a_trading_day():
    assert is_trading_day(date(2026, 9, 21)) is True  # Monday, ordinary day


def test_coverage_error_outside_known_years():
    with pytest.raises(CalendarCoverageError):
        is_trading_day(date(2030, 1, 1))
    with pytest.raises(CalendarCoverageError):
        is_trading_day(date(2024, 1, 1))


def test_previous_trading_day_skips_weekend_and_holiday():
    # 27 Jan 2026 (Tuesday) -- previous is 26 Jan (holiday), so skip to 23 Jan (Friday).
    assert previous_trading_day(date(2026, 1, 27)) == date(2026, 1, 23)


def test_next_trading_day_skips_weekend():
    # Friday 2026-09-18 -> next trading day is Monday 2026-09-21.
    assert next_trading_day(date(2026, 9, 18)) == date(2026, 9, 21)


def test_market_open_close_times_are_ist_aware():
    o = market_open(date(2026, 9, 22))
    c = market_close(date(2026, 9, 22))
    assert o.tzinfo == IST
    assert c.tzinfo == IST
    assert o.hour == 9 and o.minute == 15
    assert c.hour == 15 and c.minute == 30
    assert c > o


def test_validate_research_date_valid():
    v = validate_research_date(date(2026, 9, 22))
    assert v.is_valid is True
    assert v.reason is None


def test_validate_research_date_invalid_gives_structured_info():
    """Section 19: never silently substitute -- return previous/next trading day."""
    v = validate_research_date(date(2026, 1, 26))  # Republic Day
    assert v.is_valid is False
    assert "Republic Day" in v.reason
    assert v.previous_trading_day is not None
    assert v.next_trading_day is not None
    assert v.previous_trading_day < v.requested < v.next_trading_day


def test_trading_days_in_range_excludes_weekends_and_holidays():
    days = trading_days_in_range(date(2026, 1, 23), date(2026, 1, 28))
    # 23 Fri (trading), 24 Sat, 25 Sun, 26 Mon (Republic Day), 27 Tue, 28 Wed
    assert days == [date(2026, 1, 23), date(2026, 1, 27), date(2026, 1, 28)]


def test_trading_days_in_range_rejects_end_before_start():
    with pytest.raises(ValueError):
        trading_days_in_range(date(2026, 2, 1), date(2026, 1, 1))
