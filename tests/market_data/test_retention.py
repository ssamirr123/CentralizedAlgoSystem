"""Phase 10 -- retention purge (never auto-runs)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading.database import models
from trading.market_data.retention import run_retention

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _seed(db):
    for days in (400, 200, 100, 10):
        db.add(models.MarketCandle(
            timestamp=NOW - timedelta(days=days), symbol="NIFTY", exchange="NSE",
            interval="1minute", open=1, high=1, low=1, close=1,
        ))
    c = models.OptionContract(underlying="NIFTY", exchange="NFO", provider="icici_breeze",
                              provider_token="t", symbol="NIFTY|2026-09-10|25000|CE",
                              expiry=NOW.date(), strike=25000, option_type="CE")
    db.add(c)
    db.flush()
    for days in (300, 190, 90, 5):
        db.add(models.OptionCandle(timestamp=NOW - timedelta(days=days), contract_id=c.id,
                                   open=1, high=1, low=1, close=1))
    db.commit()


def test_run_retention_defaults(db_session):
    _seed(db_session)
    res = run_retention(db_session, index_days=365, option_days=180, now=NOW)
    assert res["index_candles"] == 1   # only the 400-day row
    assert res["option_candles"] == 2  # 300 + 190
    assert db_session.query(models.MarketCandle).count() == 3
    assert db_session.query(models.OptionCandle).count() == 2
    # contract metadata untouched
    assert db_session.query(models.OptionContract).count() == 1


def test_dry_run_deletes_nothing(db_session):
    _seed(db_session)
    res = run_retention(db_session, index_days=365, option_days=180, now=NOW, dry_run=True)
    assert res["dry_run"] is True
    assert res["index_candles"] == 1 and res["option_candles"] == 2
    assert db_session.query(models.MarketCandle).count() == 4  # nothing removed
