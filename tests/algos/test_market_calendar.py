"""Shared NSE trading-day helper (2026-10-20 Dussehra holiday incident)."""

from datetime import date, datetime

from trading.common.market_calendar import is_trading_day, non_trading_reason


def test_holiday_weekend_and_normal_day():
    assert not is_trading_day(date(2026, 10, 20))   # Dussehra
    assert not is_trading_day(date(2026, 10, 24))   # Saturday
    assert is_trading_day(date(2026, 10, 21))
    assert "holiday" in non_trading_reason(date(2026, 10, 20))
    assert "weekend" in non_trading_reason(date(2026, 10, 24))
    assert non_trading_reason(date(2026, 10, 21)) == ""


def test_uncovered_year_falls_back_to_weekday():
    assert is_trading_day(date(2030, 1, 7))         # Monday
    assert not is_trading_day(date(2030, 1, 5))     # Saturday


def test_ema_and_vwap_reclaim_scheduler_skip_holiday():
    from trading.algos.EMA20_Pullback.scheduler import Scheduler as Ema
    from trading.algos.VWAP_Reclaim.scheduler import Scheduler as Vr
    for cls in (Ema, Vr):
        s = cls(lambda now: None, interval_seconds=1)
        assert not s._in_window(datetime(2026, 10, 20, 10, 0))
        assert s._in_window(datetime(2026, 10, 21, 10, 0))


def test_double_straddle_expiry_skips_holiday():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "trading" / "algos" / "DoubleStraddelAlgo"
    sys.path.insert(0, str(root))
    from strategy import expiry
    assert expiry._is_holiday(date(2026, 10, 20))
    assert not expiry._is_holiday(date(2026, 10, 19))
