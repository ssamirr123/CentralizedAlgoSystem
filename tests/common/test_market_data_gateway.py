"""Phase 16.6: trading/common/market_data_gateway.py -- the read-only,
fail-closed seam between the existing market-data provider layer and the
strategy runtime. Never calls a broker; never imports LiveAuthorization or
StrategyExecutionEngine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading.common.market_data_gateway import (
    DEFAULT_MAX_DATA_AGE_SECONDS,
    FixedMarketDataSource,
    MarketDataStatus,
    ProviderMarketDataSource,
    check_market_data,
    gather_market_data,
)
from trading.market_data.schemas import IndexQuote


def _quote(*, ltp=100.0, age_seconds=0.0, provider="test") -> IndexQuote:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return IndexQuote.build(symbol="NIFTY", ltp=ltp, provider_timestamp=ts, provider=provider)


# --------------------------------------------------------------------------- #
# check_market_data
# --------------------------------------------------------------------------- #
def test_no_data_when_instrument_missing():
    source = FixedMarketDataSource()
    result = check_market_data(source, "NIFTY")
    assert result.status == MarketDataStatus.NO_DATA
    assert result.quote is None


def test_available_for_a_fresh_valid_quote():
    source = FixedMarketDataSource({"NIFTY": _quote(ltp=24950.5, age_seconds=1.0)})
    result = check_market_data(source, "NIFTY")
    assert result.status == MarketDataStatus.AVAILABLE
    assert result.quote is not None
    assert result.age_seconds is not None and result.age_seconds < 5.0


def test_stale_when_older_than_max_age():
    source = FixedMarketDataSource({"NIFTY": _quote(age_seconds=120.0)})
    result = check_market_data(source, "NIFTY", max_age_seconds=60.0)
    assert result.status == MarketDataStatus.STALE
    assert "MARKET_DATA_STALE" in result.reason


def test_invalid_when_ltp_is_none_or_non_positive():
    source = FixedMarketDataSource({"NIFTY": _quote(ltp=None)})
    assert check_market_data(source, "NIFTY").status == MarketDataStatus.INVALID
    source2 = FixedMarketDataSource({"NIFTY": _quote(ltp=0.0)})
    assert check_market_data(source2, "NIFTY").status == MarketDataStatus.INVALID


def test_provider_error_is_caught_never_raised():
    class _BrokenSource:
        def get_quote(self, instrument):
            raise ConnectionError("simulated network failure")

    result = check_market_data(_BrokenSource(), "NIFTY")
    assert result.status == MarketDataStatus.PROVIDER_ERROR
    assert "PROVIDER_ERROR" in result.reason


def test_default_max_age_is_documented_and_positive():
    assert DEFAULT_MAX_DATA_AGE_SECONDS > 0


# --------------------------------------------------------------------------- #
# gather_market_data -- fail closed, no partial snapshots
# --------------------------------------------------------------------------- #
def test_gather_returns_none_with_no_instruments_required():
    snapshot, results = gather_market_data(FixedMarketDataSource(), (), )
    assert snapshot is None
    assert results == []


def test_gather_returns_none_when_no_source_configured():
    snapshot, results = gather_market_data(None, ("NIFTY",))
    assert snapshot is None
    assert results[0].status == MarketDataStatus.NO_DATA


def test_gather_returns_snapshot_when_all_instruments_available():
    source = FixedMarketDataSource({
        "NIFTY": _quote(ltp=24950.0), "NIFTY24950CE": _quote(ltp=120.0),
    })
    snapshot, results = gather_market_data(source, ("NIFTY", "NIFTY24950CE"))
    assert snapshot is not None
    assert set(snapshot) == {"NIFTY", "NIFTY24950CE"}
    assert all(r.status == MarketDataStatus.AVAILABLE for r in results)


def test_gather_fails_closed_on_partial_data():
    """spot present, CE missing -- must be None, never a partial snapshot."""
    source = FixedMarketDataSource({"NIFTY": _quote(ltp=24950.0)})
    snapshot, results = gather_market_data(source, ("NIFTY", "NIFTY24950CE"))
    assert snapshot is None
    statuses = {r.instrument: r.status for r in results}
    assert statuses["NIFTY"] == MarketDataStatus.AVAILABLE
    assert statuses["NIFTY24950CE"] == MarketDataStatus.NO_DATA


def test_gather_fails_closed_when_one_of_several_is_stale():
    source = FixedMarketDataSource({
        "NIFTY": _quote(ltp=24950.0, age_seconds=1.0),
        "NIFTY24950CE": _quote(ltp=120.0, age_seconds=999.0),
    })
    snapshot, _ = gather_market_data(source, ("NIFTY", "NIFTY24950CE"), max_age_seconds=60.0)
    assert snapshot is None


# --------------------------------------------------------------------------- #
# ProviderMarketDataSource
# --------------------------------------------------------------------------- #
def test_provider_market_data_source_delegates_to_get_index_quote():
    class _FakeProvider:
        def get_index_quote(self, instrument):
            assert instrument == "NIFTY"
            return _quote(ltp=1.0)

    source = ProviderMarketDataSource(_FakeProvider())
    quote = source.get_quote("NIFTY")
    assert quote is not None


def test_provider_market_data_source_never_calls_a_mutation_method():
    class _AssertingProvider:
        def get_index_quote(self, instrument):
            return _quote()

        def place_order(self, *a, **kw):
            raise AssertionError("market data source must never call place_order")

    source = ProviderMarketDataSource(_AssertingProvider())
    source.get_quote("NIFTY")  # must not raise


# --------------------------------------------------------------------------- #
# Structural safety
# --------------------------------------------------------------------------- #
def test_module_never_touches_broker_execution_or_live_authorization():
    import inspect
    import trading.common.market_data_gateway as mod

    source = inspect.getsource(mod)
    for forbidden in (
        "place_order", "modify_order", "cancel_order", "StrategyExecutionEngine",
        "smart_api", "SmartConnect", "dhanhq", "breeze_connect",
    ):
        assert forbidden not in source, forbidden
    assert "live_authorization" not in source.lower()
