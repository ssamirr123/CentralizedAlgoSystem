"""Phase 4 / Phase 15 -- timezone-aware market-time logic."""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from trading.market_data import market_hours as mh

IST = ZoneInfo("Asia/Kolkata")
START = time(9, 10)
STOP = time(15, 45)

MON = date(2026, 9, 7)   # Monday
SAT = date(2026, 9, 12)  # Saturday
SUN = date(2026, 9, 13)  # Sunday
TUE = date(2026, 9, 8)


def test_weekday_anchor():
    assert MON.weekday() == 0 and SAT.weekday() == 5 and SUN.weekday() == 6


def test_parse_hhmm():
    assert mh.parse_hhmm("09:10") == time(9, 10)
    assert mh.parse_hhmm(" 15:45 ") == time(15, 45)
    with pytest.raises(ValueError):
        mh.parse_hhmm("9-10")
    with pytest.raises(ValueError):
        mh.parse_hhmm("25:00")


def test_parse_holidays():
    assert mh.parse_holidays("2026-01-26, 2026-03-25;2026-08-15") == {
        date(2026, 1, 26), date(2026, 3, 25), date(2026, 8, 15)
    }
    assert mh.parse_holidays("") == set()
    assert mh.parse_holidays(None) == set()
    assert mh.parse_holidays("garbage,2026-05-01") == {date(2026, 5, 1)}


def test_is_trading_day():
    assert mh.is_trading_day(MON) is True
    assert mh.is_trading_day(SAT) is False
    assert mh.is_trading_day(SUN) is False
    assert mh.is_trading_day(MON, {MON}) is False  # holiday


@pytest.mark.parametrize(
    "hh,mm,expected",
    [
        (8, 59, False),
        (9, 9, False),
        (9, 10, True),   # exactly at open
        (9, 11, True),
        (15, 44, True),
        (15, 45, False),  # exactly at close -> closed
        (15, 46, False),
        (23, 0, False),
    ],
)
def test_market_is_open_boundaries(hh, mm, expected):
    now = datetime(2026, 9, 7, hh, mm, tzinfo=IST)
    assert mh.market_is_open(now, start=START, stop=STOP, tz=IST) is expected


def test_market_is_open_weekend_and_holiday():
    noon_sat = datetime(2026, 9, 12, 12, 0, tzinfo=IST)
    assert mh.market_is_open(noon_sat, start=START, stop=STOP, tz=IST) is False
    noon_mon = datetime(2026, 9, 7, 12, 0, tzinfo=IST)
    assert mh.market_is_open(noon_mon, start=START, stop=STOP, tz=IST, holidays={MON}) is False


def test_market_is_open_accepts_utc_input():
    # 09:30 IST == 04:00 UTC
    now_utc = datetime(2026, 9, 7, 4, 0, tzinfo=ZoneInfo("UTC"))
    assert mh.market_is_open(now_utc, start=START, stop=STOP, tz=IST) is True


def test_next_market_start_same_day_before_open():
    now = datetime(2026, 9, 7, 8, 0, tzinfo=IST)
    nxt = mh.next_market_start(now, start=START, stop=STOP, tz=IST)
    assert nxt == datetime(2026, 9, 7, 9, 10, tzinfo=IST)


def test_next_market_start_after_close_rolls_to_next_trading_day():
    fri = datetime(2026, 9, 11, 16, 0, tzinfo=IST)  # Friday after close
    nxt = mh.next_market_start(fri, start=START, stop=STOP, tz=IST)
    assert nxt == datetime(2026, 9, 14, 9, 10, tzinfo=IST)  # skips Sat+Sun -> Monday


def test_next_market_start_skips_holiday():
    now = datetime(2026, 9, 7, 8, 0, tzinfo=IST)
    nxt = mh.next_market_start(now, start=START, stop=STOP, tz=IST, holidays={MON, TUE})
    assert nxt == datetime(2026, 9, 9, 9, 10, tzinfo=IST)


def test_market_close():
    now = datetime(2026, 9, 7, 10, 0, tzinfo=IST)
    assert mh.market_close(now, start=START, stop=STOP, tz=IST) == datetime(2026, 9, 7, 15, 45, tzinfo=IST)
    sat = datetime(2026, 9, 12, 10, 0, tzinfo=IST)
    assert mh.market_close(sat, start=START, stop=STOP, tz=IST) is None
