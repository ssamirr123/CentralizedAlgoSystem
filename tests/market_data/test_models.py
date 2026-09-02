"""Phase 9 -- market-data schema: inserts, unique constraints, aggregator persistence."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from trading.database import models
from trading.market_data.aggregator import Candle, persist_index_candles, persist_option_candles

T = datetime(2026, 9, 7, 9, 15, tzinfo=timezone.utc)


def test_market_candle_insert_and_duplicate_rejected(db_session):
    row = models.MarketCandle(timestamp=T, symbol="NIFTY", exchange="NSE", interval="1minute",
                              open=1, high=2, low=0.5, close=1.5)
    db_session.add(row)
    db_session.commit()
    dup = models.MarketCandle(timestamp=T, symbol="NIFTY", exchange="NSE", interval="1minute",
                              open=9, high=9, low=9, close=9)
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_option_contract_natural_key_and_symbol_unique(db_session):
    c1 = models.OptionContract(underlying="NIFTY", exchange="NFO", provider="icici_breeze",
                               provider_token="111", symbol="NIFTY|2026-09-10|25000|CE",
                               expiry=date(2026, 9, 10), strike=25000, option_type="CE", lot_size=75)
    db_session.add(c1)
    db_session.commit()
    dup_symbol = models.OptionContract(underlying="NIFTY", exchange="NFO", provider="icici_breeze",
                                       provider_token="222", symbol="NIFTY|2026-09-10|25000|CE",
                                       expiry=date(2026, 9, 10), strike=25000, option_type="CE")
    db_session.add(dup_symbol)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_option_candle_dup_rejected(db_session):
    c = models.OptionContract(underlying="NIFTY", exchange="NFO", provider="icici_breeze",
                              provider_token="tok", symbol="NIFTY|2026-09-10|25000|PE",
                              expiry=date(2026, 9, 10), strike=25000, option_type="PE")
    db_session.add(c)
    db_session.commit()
    db_session.add(models.OptionCandle(timestamp=T, contract_id=c.id, open=1, high=1, low=1, close=1))
    db_session.commit()
    db_session.add(models.OptionCandle(timestamp=T, contract_id=c.id, open=2, high=2, low=2, close=2))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_persist_index_candles_is_idempotent(db_session):
    items = [
        ("NIFTY", "NSE", Candle("NIFTY", "1minute", T, 1, 2, 0.5, 1.5, volume=100)),
        ("NIFTY", "NSE", Candle("NIFTY", "1minute", T + timedelta(minutes=1), 1.5, 2, 1, 1.8)),
    ]
    assert persist_index_candles(db_session, items) == 2
    # second call: both already exist -> 0 new rows, no error
    assert persist_index_candles(db_session, items) == 0
    assert db_session.query(models.MarketCandle).count() == 2


def test_persist_option_candles_is_idempotent(db_session):
    c = models.OptionContract(underlying="NIFTY", exchange="NFO", provider="icici_breeze",
                              provider_token="z", symbol="NIFTY|2026-09-10|25100|CE",
                              expiry=date(2026, 9, 10), strike=25100, option_type="CE")
    db_session.add(c)
    db_session.commit()
    items = [(c.id, Candle("x", "1minute", T, 10, 12, 9, 11, oi=5000))]
    assert persist_option_candles(db_session, items) == 1
    assert persist_option_candles(db_session, items) == 0
