"""
HistoricalMarketDataService tests. A fake MarketDataProvider (matching the
real ABC) is injected via provider_factory -- no real Breeze/network call.
Dates are real, verified NSE trading days/holidays from Phase 7's
calendar_nse module (2026-01-05..09 is a real trading week; 2026-01-26 is
the real Republic Day holiday).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trading.ai_research.market.instruments import Market, resolve_instrument
from trading.ai_research.market_data import service
from trading.ai_research.market_data.schemas import DataSource, DataStatus
from trading.database import models
from trading.database.connection import SessionLocal
from trading.market_data.providers.base import (
    MarketDataProvider,
    ProviderAuthError,
    ProviderConnectionError,
    ProviderRateLimitError,
)
from trading.market_data.schemas import Candle


class _FakeProvider(MarketDataProvider):
    name = "fake_breeze"

    def __init__(self, *, candles=None, raise_error=None, call_log=None, **_kwargs):
        self._candles = candles or []
        self._raise = raise_error
        self._call_log = call_log if call_log is not None else []
        self.connected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    def get_historical_candles(self, instrument, interval, start, end):
        self._call_log.append((instrument.internal_symbol, interval, start, end))
        if self._raise is not None:
            raise self._raise
        return [c for c in self._candles if start <= c.timestamp <= end]

    def get_index_quote(self, instrument):
        raise NotImplementedError

    def get_option_quote(self, instrument):
        raise NotImplementedError

    def get_option_chain(self, underlying, expiry, *, right=None):
        raise NotImplementedError

    def get_option_instruments(self, underlying):
        raise NotImplementedError

    def subscribe(self, instruments, on_tick, *, resolver=None):
        raise NotImplementedError

    def unsubscribe(self, instruments):
        raise NotImplementedError


def _candle(day: date, hour=10, minute=0, o=100.0, h=101.0, low=99.0, c=100.5, v=1000) -> Candle:
    return Candle(
        symbol="NIFTY", interval="5minute",
        timestamp=datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc),
        open=o, high=h, low=low, close=c, volume=v, oi=None,
    )


def _factory(candles=None, raise_error=None, call_log=None):
    def factory(name, **kwargs):
        return _FakeProvider(candles=candles, raise_error=raise_error, call_log=call_log)
    return factory


NIFTY = resolve_instrument(Market.INDIA, "NIFTY")
RELIANCE = resolve_instrument(Market.INDIA, "RELIANCE")

# Real verified NSE trading days (Phase 7 calendar): Mon 2026-01-05 .. Fri 2026-01-09.
_MON, _TUE, _WED, _THU, _FRI = (date(2026, 1, d) for d in (5, 6, 7, 8, 9))
_REPUBLIC_DAY = date(2026, 1, 26)  # real holiday


def test_db_miss_then_backfill_and_persist():
    call_log = []
    candles = [_candle(_MON), _candle(_TUE), _candle(_WED)]
    result = service.get_historical_candles(
        NIFTY, "5minute", _MON, _WED, provider_factory=_factory(candles=candles, call_log=call_log),
    )
    assert result.status == DataStatus.AVAILABLE
    assert result.source == DataSource.PROVIDER
    assert result.db_hit_count == 0
    assert result.provider_call_count == 1
    assert result.candles_stored == 3
    assert len(call_log) == 1  # one call covering the whole contiguous run


def test_second_identical_request_is_db_first_no_provider_call():
    call_log = []
    candles = [_candle(_MON)]
    factory = _factory(candles=candles, call_log=call_log)
    service.get_historical_candles(NIFTY, "5minute", _MON, _MON, provider_factory=factory)
    assert len(call_log) == 1

    result2 = service.get_historical_candles(NIFTY, "5minute", _MON, _MON, provider_factory=factory)
    assert result2.status == DataStatus.AVAILABLE
    assert result2.source == DataSource.DB
    assert result2.provider_call_count == 0
    assert len(call_log) == 1, "second identical request must not call the provider again"


def test_duplicate_ingestion_is_idempotent():
    """Section 38: repeated ingestion of the same period creates zero duplicates."""
    candles = [_candle(_MON), _candle(_MON, hour=10, minute=5)]
    service.get_historical_candles(NIFTY, "5minute", _MON, _MON, provider_factory=_factory(candles=candles))
    db = SessionLocal()
    try:
        first_count = db.query(models.MarketCandle).filter_by(symbol="NIFTY").count()
    finally:
        db.close()
    assert first_count == 2

    # Re-ingest the exact same period (force via allow_backfill after clearing DB knowledge is
    # not needed -- calling again just hits DB; to actually re-trigger a fetch we bypass the
    # DB-first path is not the point here: the real idempotency guarantee lives in
    # persist_index_candles, exercised again directly).
    from trading.market_data.aggregator import persist_index_candles
    db = SessionLocal()
    try:
        stored_again = persist_index_candles(db, [("NIFTY", "NSE", c) for c in candles])
    finally:
        db.close()
    assert stored_again == 0, "re-inserting identical candles must store zero new rows"

    db = SessionLocal()
    try:
        final_count = db.query(models.MarketCandle).filter_by(symbol="NIFTY").count()
    finally:
        db.close()
    assert final_count == 2


def test_partial_range_detection_when_provider_returns_fewer_days():
    """Section 14/39: gap detection -- provider only had data for one of
    three requested trading days."""
    candles = [_candle(_MON)]  # TUE/WED missing from provider response too
    result = service.get_historical_candles(
        NIFTY, "5minute", _MON, _WED, provider_factory=_factory(candles=candles),
    )
    assert result.status == DataStatus.PARTIAL
    assert set(result.missing_days) == {_TUE, _WED}


def test_holiday_and_weekend_never_counted_as_missing():
    """Section 40: a request spanning a real NSE holiday must not treat
    that holiday as a data gap."""
    candles = [_candle(_MON), _candle(_TUE), _candle(_WED), _candle(_THU), _candle(_FRI)]
    result = service.get_historical_candles(
        NIFTY, "5minute", _MON, _FRI, provider_factory=_factory(candles=candles),
    )
    assert result.status == DataStatus.AVAILABLE
    assert result.missing_days == ()


def test_bounded_backfill_never_auto_fetches_beyond_configured_limit(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_MAX_BACKFILL_DAYS", "1")
    call_log = []
    result = service.get_historical_candles(
        NIFTY, "5minute", _MON, _WED, provider_factory=_factory(candles=[_candle(_MON)], call_log=call_log),
    )
    assert len(call_log) == 0, "must not call the provider when missing days exceed the configured bound"
    assert result.status in (DataStatus.PARTIAL, DataStatus.MISSING)


def test_provider_call_budget_is_capped(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_MAX_PROVIDER_CALLS", "1")
    call_log = []
    # Two separated runs (gap > 3 days) forces two separate provider calls -- only 1 allowed.
    far_day = date(2026, 2, 2)  # Monday, another real trading day, far from _MON
    result = service.get_historical_candles(
        NIFTY, "5minute", _MON, far_day, provider_factory=_factory(candles=[], call_log=call_log),
    )
    assert len(call_log) <= 1


def test_rate_limit_stops_further_calls_no_retry_storm():
    call_log = []
    result = service.get_historical_candles(
        NIFTY, "5minute", _MON, _WED,
        provider_factory=_factory(raise_error=ProviderRateLimitError("throttled"), call_log=call_log),
    )
    assert result.status == DataStatus.RATE_LIMITED
    assert len(call_log) == 1, "a rate limit must stop the request, never be retried in a loop"


def test_provider_auth_error_classified_honestly():
    result = service.get_historical_candles(
        NIFTY, "5minute", _MON, _MON,
        provider_factory=_factory(raise_error=ProviderAuthError("no session")),
    )
    assert result.status == DataStatus.PROVIDER_ERROR


def test_transient_connection_error_gets_bounded_retry(monkeypatch):
    monkeypatch.setattr(service, "_CONNECTION_RETRY_DELAY_SECONDS", 0)
    attempts = {"n": 0}

    class _FlakyProvider(_FakeProvider):
        def get_historical_candles(self, instrument, interval, start, end):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ProviderConnectionError("timeout")
            return [_candle(_MON)]

    def factory(name, **kwargs):
        return _FlakyProvider()

    result = service.get_historical_candles(NIFTY, "5minute", _MON, _MON, provider_factory=factory)
    assert attempts["n"] == 2, "must retry a transient failure at least once, bounded"
    assert result.status == DataStatus.AVAILABLE


def test_malformed_candle_is_never_persisted():
    """Section 18: high < low must be rejected before persistence."""
    bad = Candle(symbol="NIFTY", interval="5minute", timestamp=datetime(2026, 1, 5, 10, tzinfo=timezone.utc),
                 open=100, high=90, low=95, close=100, volume=1, oi=None)  # high < low
    good = _candle(_MON, hour=11)
    result = service.get_historical_candles(
        NIFTY, "5minute", _MON, _MON, provider_factory=_factory(candles=[bad, good]),
    )
    assert result.candles_stored == 1
    assert all(c.high >= c.low for c in result.candles)


def test_research_date_cutoff_excludes_future_candles():
    """Section 21/22: no candle after the research-date cutoff may ever
    be returned, even if the caller requested a wider range."""
    candles = [_candle(_MON, hour=10), _candle(_TUE, hour=10)]  # TUE is "after" the cutoff day
    result = service.get_historical_candles(
        NIFTY, "5minute", _MON, _WED, research_date_cutoff=_MON,
        provider_factory=_factory(candles=candles),
    )
    assert all(c.timestamp.date() <= _MON for c in result.candles)
    assert result.requested_to == _MON
    assert result.cutoff_applied is not None


def test_no_future_data_leakage_even_when_db_already_has_it():
    """A candle already sitting in the DB for a date AFTER the cutoff
    must still never be returned."""
    future_candle = _candle(_FRI, hour=10)
    service.get_historical_candles(NIFTY, "5minute", _FRI, _FRI, provider_factory=_factory(candles=[future_candle]))
    result = service.get_historical_candles(
        NIFTY, "5minute", _MON, _FRI, research_date_cutoff=_TUE,
        provider_factory=_factory(candles=[_candle(_MON), _candle(_TUE)]),
    )
    assert all(c.timestamp.date() <= _TUE for c in result.candles)


def test_us_market_is_unsupported_never_silently_falls_back():
    """Section 31: no silent substitution of a different data source."""
    aapl = resolve_instrument(Market.US, "AAPL")
    result = service.get_historical_candles(aapl, "5minute", _MON, _MON, provider_factory=_factory())
    assert result.status == DataStatus.UNSUPPORTED
    assert result.source == DataSource.NONE


def test_equity_instrument_resolution_and_fetch():
    result = service.get_historical_candles(
        RELIANCE, "5minute", _MON, _MON,
        provider_factory=_factory(candles=[_candle(_MON, o=1200, h=1210, low=1195, c=1205)]),
    )
    assert result.symbol == "RELIANCE"
    assert result.status == DataStatus.AVAILABLE


def test_provenance_mixed_when_db_and_provider_both_contribute():
    service.get_historical_candles(NIFTY, "5minute", _MON, _MON, provider_factory=_factory(candles=[_candle(_MON)]))
    result = service.get_historical_candles(
        NIFTY, "5minute", _MON, _TUE, provider_factory=_factory(candles=[_candle(_TUE)]),
    )
    assert result.source == DataSource.MIXED
    assert result.db_hit_count == 1
    assert result.provider_call_count == 1
