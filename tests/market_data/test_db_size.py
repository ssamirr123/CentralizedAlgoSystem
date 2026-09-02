"""Phase 21 -- database-size simulation.

Simulate one market session of ticks for 4 indices + a NIFTY option
universe and assert PostgreSQL receives ONE row per minute per series,
not one per tick. Report the estimated footprint.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading.database import models
from trading.market_data.aggregator import CandleAggregator, persist_index_candles, persist_option_candles

SESSION_MINUTES = 375           # 09:10 -> 15:45 IST
TICKS_PER_MINUTE = 100          # busy market
INDICES = [("NIFTY", "NSE"), ("BANKNIFTY", "NSE"), ("INDIA_VIX", "NSE"), ("SENSEX", "BSE")]
OPTION_CONTRACTS = 42           # ATM +- 10 CE/PE + a little slack

BASE = datetime(2026, 9, 7, 3, 40, tzinfo=timezone.utc)  # ~09:10 IST


def test_one_row_per_minute_not_per_tick(db_session, capsys):
    from datetime import date

    from trading.database import models as m

    # option_candles has an FK to option_contracts -- seed the universe
    for cid in range(1, OPTION_CONTRACTS + 1):
        db_session.add(m.OptionContract(
            underlying="NIFTY", exchange="NFO", provider="icici_breeze", provider_token=f"tok{cid}",
            symbol=f"NIFTY|2026-09-10|{24000 + cid * 50}|CE", expiry=date(2026, 9, 10),
            strike=24000 + cid * 50, option_type="CE",
        ))
    db_session.commit()

    agg = CandleAggregator()
    total_ticks = 0

    for minute in range(SESSION_MINUTES):
        t0 = BASE + timedelta(minutes=minute)
        for tick in range(TICKS_PER_MINUTE):
            ts = t0 + timedelta(seconds=(tick * 60 // TICKS_PER_MINUTE))
            px = 25000 + (minute % 50) + tick * 0.01
            for sym, exch in INDICES:
                agg.on_index_tick(sym, exch, px, ts, volume=1000 + tick)
                total_ticks += 1
            for cid in range(1, OPTION_CONTRACTS + 1):
                agg.on_option_tick(cid, 100 + (cid % 7) + tick * 0.02, ts, oi=10_000 + tick)
                total_ticks += 1
        idx, opt = agg.flush(t0 + timedelta(minutes=1, seconds=1))
        persist_index_candles(db_session, idx)
        persist_option_candles(db_session, opt)

    # close the final minute
    idx, opt = agg.force_close_all(BASE + timedelta(minutes=SESSION_MINUTES + 1))
    persist_index_candles(db_session, idx)
    persist_option_candles(db_session, opt)

    idx_rows = db_session.query(models.MarketCandle).count()
    opt_rows = db_session.query(models.OptionCandle).count()

    assert idx_rows == SESSION_MINUTES * len(INDICES)
    assert opt_rows == SESSION_MINUTES * OPTION_CONTRACTS

    rows_per_day = idx_rows + opt_rows
    assert rows_per_day < total_ticks / 50  # >50x compression vs storing ticks

    approx_bytes_per_row = 120
    per_month = rows_per_day * 22
    with capsys.disabled():
        print(
            f"\n[db-size] ticks/day={total_ticks:,} -> rows/day={rows_per_day:,} "
            f"({total_ticks / rows_per_day:.0f}x compression)\n"
            f"[db-size] rows/month~{per_month:,} -> ~{per_month * approx_bytes_per_row / 1e6:.1f} MB/month "
            f"(index {idx_rows}/day, options {opt_rows}/day)"
        )
