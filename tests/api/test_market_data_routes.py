"""
Phase 8 Market Data API tests. The Breeze provider is monkeypatched at
the service's provider_factory seam -- no real network/Breeze call.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trading.ai_research.market_data import service as market_data_service
from trading.market_data.providers.base import MarketDataProvider
from trading.market_data.schemas import Candle

_MON, _TUE, _WED = (date(2026, 1, d) for d in (5, 6, 7))


class _FakeProvider(MarketDataProvider):
    name = "fake"

    def __init__(self, candles=None, **_kwargs):
        self._candles = candles or []

    def connect(self):
        pass

    def disconnect(self):
        pass

    def is_connected(self):
        return True

    def get_historical_candles(self, instrument, interval, start, end):
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


def _candle(day: date) -> Candle:
    return Candle(symbol="NIFTY", interval="5minute", timestamp=datetime(day.year, day.month, day.day, 10, tzinfo=timezone.utc),
                  open=100.0, high=101.0, low=99.0, close=100.5, volume=10, oi=None)


@pytest.fixture(autouse=True)
def _fake_factory(monkeypatch):
    def factory(name, **kwargs):
        return _FakeProvider(candles=[_candle(_MON), _candle(_TUE), _candle(_WED)])
    monkeypatch.setattr("trading.ai_research.market_data.router.get_historical_candles",
                         lambda *a, **k: market_data_service.get_historical_candles(*a, **k, provider_factory=factory))
    yield


def test_requires_auth(client):
    r = client.get("/api/market-data/candles", params={
        "market": "INDIA", "instrument": "NIFTY", "from": "2026-01-05", "to": "2026-01-07",
    })
    assert r.status_code == 401


def test_get_candles_returns_provenance_and_candles(client, viewer_auth):
    r = client.get("/api/market-data/candles", headers=viewer_auth, params={
        "market": "INDIA", "instrument": "NIFTY", "interval": "5minute",
        "from": "2026-01-05", "to": "2026-01-07",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "AVAILABLE"
    assert body["source"] == "ICICI_BREEZE"
    assert body["provider"] == "icici_breeze"
    assert body["symbol"] == "NIFTY"
    assert body["candle_count"] == 3
    assert len(body["candles"]) == 3


def test_status_endpoint_never_triggers_a_fetch(client, viewer_auth):
    """Section 34: a diagnostic status read must never itself perform
    ingestion work."""
    r = client.get("/api/market-data/status", headers=viewer_auth, params={
        "market": "INDIA", "instrument": "NIFTY", "interval": "5minute",
        "from": "2026-01-05", "to": "2026-01-07",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["candles"] == []  # never ships the payload
    assert body["provider_call_count"] == 0
    assert body["status"] in ("PARTIAL", "MISSING")


def test_unsupported_interval_rejected(client, viewer_auth):
    r = client.get("/api/market-data/candles", headers=viewer_auth, params={
        "market": "INDIA", "instrument": "NIFTY", "interval": "15minute",
        "from": "2026-01-05", "to": "2026-01-05",
    })
    assert r.status_code == 422


def test_invalid_instrument_rejected(client, viewer_auth):
    r = client.get("/api/market-data/candles", headers=viewer_auth, params={
        "market": "INDIA", "instrument": "bad symbol!", "from": "2026-01-05", "to": "2026-01-05",
    })
    assert r.status_code == 422


def test_us_market_rejected_no_silent_fallback(client, viewer_auth):
    """Section 31: no silent substitution -- market=US gets an honest
    UNSUPPORTED diagnostic (200, per Section 30's diagnostic philosophy --
    the same pattern ResearchDisabledOut already uses), never a fabricated
    result and never a fallback to a different data source."""
    r = client.get("/api/market-data/candles", headers=viewer_auth, params={
        "market": "US", "instrument": "AAPL", "from": "2026-01-05", "to": "2026-01-05",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "UNSUPPORTED"
    assert body["source"] == "NONE"
    assert body["candles"] == []


def test_credentials_never_appear_in_response(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("BREEZE_API_KEY", "super-secret-key")
    monkeypatch.setenv("BREEZE_SECRET_KEY", "super-secret-secret")
    monkeypatch.setenv("BREEZE_SESSION_TOKEN", "super-secret-token")
    r = client.get("/api/market-data/candles", headers=viewer_auth, params={
        "market": "INDIA", "instrument": "NIFTY", "from": "2026-01-05", "to": "2026-01-07",
    })
    body_text = r.text
    assert "super-secret-key" not in body_text
    assert "super-secret-secret" not in body_text
    assert "super-secret-token" not in body_text
