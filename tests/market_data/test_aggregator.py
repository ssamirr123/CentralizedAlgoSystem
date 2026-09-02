"""Phase 8 -- 1-minute aggregation: one row per minute, never per tick."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading.market_data.aggregator import CandleAggregator

BASE = datetime(2026, 9, 7, 9, 15, 0, tzinfo=timezone.utc)


def test_ticks_collapse_into_one_minute_candle():
    agg = CandleAggregator()
    # 5 ticks in the 09:15 minute
    for i, px in enumerate([100, 105, 98, 103, 101]):
        agg.on_index_tick("NIFTY", "NSE", px, BASE + timedelta(seconds=i * 10), volume=1000 + i)
    # nothing flushes while 09:15 is still current
    idx, opt = agg.flush(BASE + timedelta(seconds=30))
    assert idx == [] and opt == []
    # once 09:16 arrives, the 09:15 candle closes
    agg.on_index_tick("NIFTY", "NSE", 110, BASE + timedelta(minutes=1, seconds=1))
    idx, _ = agg.flush(BASE + timedelta(minutes=1, seconds=5))
    assert len(idx) == 1
    symbol, exch, c = idx[0]
    assert symbol == "NIFTY" and exch == "NSE"
    assert (c.open, c.high, c.low, c.close) == (100, 105, 98, 101)
    assert c.timestamp == BASE  # minute start
    assert c.volume == 1004  # last cumulative volume wins


def test_multiple_minutes_and_symbols():
    agg = CandleAggregator()
    agg.on_index_tick("NIFTY", "NSE", 100, BASE)
    agg.on_index_tick("NIFTY", "NSE", 101, BASE + timedelta(minutes=1))
    agg.on_index_tick("BANKNIFTY", "NSE", 500, BASE)
    agg.on_index_tick("BANKNIFTY", "NSE", 502, BASE + timedelta(minutes=2))
    idx, _ = agg.flush(BASE + timedelta(minutes=3))
    # NIFTY 09:15 + 09:16, BANKNIFTY 09:15 + 09:17
    assert len(idx) == 4
    minutes = sorted({c.timestamp for _, _, c in idx})
    assert minutes == [BASE, BASE + timedelta(minutes=1), BASE + timedelta(minutes=2)]


def test_option_ticks_keyed_by_contract_id():
    agg = CandleAggregator()
    agg.on_option_tick(42, 120.0, BASE, oi=10000)
    agg.on_option_tick(42, 125.0, BASE + timedelta(seconds=20), oi=10500)
    agg.on_option_tick(42, 118.0, BASE + timedelta(seconds=40), oi=10200)  # same 09:15 minute
    agg.on_option_tick(42, 130.0, BASE + timedelta(minutes=1, seconds=1))  # rolls to 09:16
    _, opt = agg.flush(BASE + timedelta(minutes=1, seconds=5))
    assert len(opt) == 1
    cid, c = opt[0]
    assert cid == 42
    assert (c.open, c.high, c.low, c.close) == (120.0, 125.0, 118.0, 118.0)
    assert c.oi == 10200


def test_late_tick_for_closed_minute_is_dropped():
    agg = CandleAggregator()
    agg.on_index_tick("NIFTY", "NSE", 100, BASE)
    agg.on_index_tick("NIFTY", "NSE", 101, BASE + timedelta(minutes=2))
    idx, _ = agg.flush(BASE + timedelta(minutes=3))
    # a straggler for 09:15 arrives after it already closed
    agg.on_index_tick("NIFTY", "NSE", 999, BASE + timedelta(seconds=5))
    idx2, _ = agg.flush(BASE + timedelta(minutes=4))
    assert all(c.high != 999 for _, _, c in idx + idx2)


def test_force_close_all_emits_current_minute():
    agg = CandleAggregator()
    agg.on_index_tick("NIFTY", "NSE", 100, BASE)
    idx, _ = agg.force_close_all(BASE + timedelta(seconds=10))
    assert len(idx) == 1 and idx[0][2].close == 100
