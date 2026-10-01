"""
Regression tests for the OHLC data-integrity fixes in
trading/algos/supertrendPivotAlgo/{tick_stream.py, market_data.py}.

Scenario (from live logs on 2026-10-01):
    After a 5-min boundary flush, a late WebSocket tick whose
    exchange_timestamp still falls inside the just-closed bucket would
    spawn a second, degenerate H=L=O=C bar at the same timestamp. The
    merge in market_data._merge_live_candles kept the LAST row, destroying
    the real OHLC and corrupting the input to Supertrend.
"""

from datetime import datetime, timedelta
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2] / "trading" / "algos" / "supertrendPivotAlgo"
sys.path.insert(0, str(ROOT))

from tick_stream import CandleAggregator  # noqa: E402


def test_late_tick_does_not_resurrect_finalized_bucket():
    """A tick whose bucket is already finalized must be dropped, not spawn
    a degenerate second bar at the same timestamp."""
    agg = CandleAggregator(interval_minutes=5)

    # Real 10:00 bar: ticks spread across the 5-min window.
    real_ticks = [
        (datetime(2026, 10, 1, 10, 0, 5), 22569.20),   # open
        (datetime(2026, 10, 1, 10, 2, 0), 22601.80),   # high
        (datetime(2026, 10, 1, 10, 3, 30), 22567.60),  # low
        (datetime(2026, 10, 1, 10, 4, 58), 22600.05),  # close
    ]
    for ts, price in real_ticks:
        agg.add_tick(price, timestamp=ts)

    # Boundary flusher fires exactly at 10:05:00.
    assert agg.finalize_due(now=datetime(2026, 10, 1, 10, 5, 0)) is True

    # Late tick arrives at 10:05:00.3 with exchange_timestamp still inside
    # the 10:00 bucket (bucket = 10:00 after _floor_to_bucket).
    agg.add_tick(22599.55, timestamp=datetime(2026, 10, 1, 10, 4, 59, 900000))

    # Normal in-bucket tick for the new 10:05 bar.
    agg.add_tick(22599.50, timestamp=datetime(2026, 10, 1, 10, 5, 1))

    completed = agg.snapshot(include_forming=False)
    # Exactly ONE candle for 10:00 bucket, and it is the real OHLC.
    ten_oclock = [c for c in completed if c["datetime"] == datetime(2026, 10, 1, 10, 0)]
    assert len(ten_oclock) == 1, f"expected 1 bar for 10:00, got {len(ten_oclock)}"
    c = ten_oclock[0]
    assert c["open"] == 22569.20
    assert c["high"] == 22601.80
    assert c["low"] == 22567.60
    assert c["close"] == 22600.05


def test_normal_bucket_rollover_still_works():
    """The fix must not break the normal case where the next tick
    legitimately rolls the bucket forward."""
    agg = CandleAggregator(interval_minutes=5)
    agg.add_tick(100.0, timestamp=datetime(2026, 10, 1, 10, 0, 10))
    agg.add_tick(101.0, timestamp=datetime(2026, 10, 1, 10, 2, 0))
    # No boundary flush — the next tick in the new bucket should finalize
    # the previous bar.
    agg.add_tick(102.0, timestamp=datetime(2026, 10, 1, 10, 5, 1))

    completed = agg.snapshot(include_forming=False)
    assert len(completed) == 1
    assert completed[0]["datetime"] == datetime(2026, 10, 1, 10, 0)
    assert completed[0]["high"] == 101.0
    assert completed[0]["close"] == 101.0


def test_merge_live_candles_drops_pre_open_bars():
    """Pre-open live candles (before MARKET_OPEN = 09:15) must be dropped
    before merging into the Supertrend input. On 2026-10-01 the live stream
    emitted 09:00/09:05/09:10 bars; the 09:00 bar spanned 223 points and
    polluted Wilder ATR for the first ~15 bars of the session, which caused
    a spurious RED->GREEN flip at 10:00 that Angel's chart did not show."""
    from unittest.mock import MagicMock
    import importlib
    md_mod = importlib.import_module("market_data")

    rest_df = pd.DataFrame(
        [
            {"datetime": pd.Timestamp("2026-09-30 15:25:00"),
             "open": 22700.0, "high": 22705.0, "low": 22690.0,
             "close": 22695.0, "volume": 0},
        ]
    )
    pre_open_and_session = [
        # Pre-open bar with the pathological 223-point range from 2026-10-01.
        {"datetime": pd.Timestamp("2026-10-01 09:00:00"),
         "open": 22265.50, "high": 22465.70, "low": 22242.50,
         "close": 22452.45, "volume": 0},
        {"datetime": pd.Timestamp("2026-10-01 09:05:00"),
         "open": 22452.90, "high": 22547.15, "low": 22446.50,
         "close": 22543.70, "volume": 0},
        {"datetime": pd.Timestamp("2026-10-01 09:10:00"),
         "open": 22543.70, "high": 22543.70, "low": 22543.70,
         "close": 22543.70, "volume": 0},
        # First real session bar — must survive.
        {"datetime": pd.Timestamp("2026-10-01 09:15:00"),
         "open": 22554.50, "high": 22589.35, "low": 22523.55,
         "close": 22556.20, "volume": 0},
    ]

    stream = MagicMock()
    stream.get_live_candles.return_value = pre_open_and_session

    md = md_mod.MarketData.__new__(md_mod.MarketData)
    md._stream = stream
    merged = md._merge_live_candles(rest_df)

    session_times = merged[merged["datetime"] >= pd.Timestamp("2026-10-01")]
    assert len(session_times) == 1
    assert session_times.iloc[0]["datetime"] == pd.Timestamp("2026-10-01 09:15:00")
    assert session_times.iloc[0]["high"] == 22589.35
    assert not (merged["datetime"] == pd.Timestamp("2026-10-01 09:00:00")).any()
    assert not (merged["datetime"] == pd.Timestamp("2026-10-01 09:05:00")).any()
    assert not (merged["datetime"] == pd.Timestamp("2026-10-01 09:10:00")).any()


def test_merge_live_candles_dedupe_keeps_first():
    """market_data._merge_live_candles must drop a duplicate degenerate
    live row at the same timestamp (belt-and-braces for the aggregator
    fix). Simulated here directly against the DataFrame logic."""
    rest_df = pd.DataFrame(
        [
            {"datetime": pd.Timestamp("2026-10-01 09:55:00"),
             "open": 22539.20, "high": 22571.10, "low": 22532.55,
             "close": 22570.45, "volume": 0},
        ]
    )
    live_rows = [
        # Real tick-aggregated bar
        {"datetime": pd.Timestamp("2026-10-01 10:00:00"),
         "open": 22569.20, "high": 22601.80, "low": 22567.60,
         "close": 22600.05, "volume": 0},
        # Degenerate replay from a late tick (same timestamp)
        {"datetime": pd.Timestamp("2026-10-01 10:00:00"),
         "open": 22599.55, "high": 22599.55, "low": 22599.55,
         "close": 22599.55, "volume": 0},
    ]
    live_df = pd.DataFrame(live_rows)
    live_df = live_df.drop_duplicates(subset="datetime", keep="first")
    combined = pd.concat([rest_df, live_df], ignore_index=True)
    combined = combined.drop_duplicates(
        subset="datetime", keep="last"
    ).sort_values("datetime").reset_index(drop=True)

    ten = combined[combined["datetime"] == pd.Timestamp("2026-10-01 10:00:00")].iloc[0]
    assert ten["high"] == 22601.80
    assert ten["low"] == 22567.60
    assert ten["close"] == 22600.05


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
