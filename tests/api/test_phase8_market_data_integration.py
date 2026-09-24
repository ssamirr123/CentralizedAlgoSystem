"""
Phase 8 end-to-end integration: an India research submission and an India
backtest cell both source real technical context through the Market Data
Service (DB-first, bounded-backfill), and the resulting provenance is
persisted and visible via the API. run_research()'s LLM call is
monkeypatched; the Breeze provider is monkeypatched via provider_factory
(no real network call anywhere in this file).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from trading.ai_research import service as research_service
from trading.ai_research.backtest import service as backtest_service
from trading.ai_research.trading_agents_adapter import ResearchReport, ResearchResult
from trading.market_data.providers.base import MarketDataProvider
from trading.market_data.schemas import Candle

_REAL_TRADING_DAY = "2026-01-27"


class _FakeProvider(MarketDataProvider):
    name = "fake"

    def __init__(self, **_kwargs):
        pass

    def connect(self):
        pass

    def disconnect(self):
        pass

    def is_connected(self):
        return True

    def get_historical_candles(self, instrument, interval, start, end):
        d = datetime(2026, 1, 27, 10, tzinfo=timezone.utc)
        if not (start <= d <= end):
            return []
        return [Candle(symbol=instrument.internal_symbol, interval=interval, timestamp=d,
                        open=23300.0, high=23350.0, low=23280.0, close=23320.0, volume=1000, oi=None)]

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


@pytest.fixture(autouse=True)
def _wiring(monkeypatch):
    research_service._reset_semaphore()
    backtest_service._reset_semaphore()
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")

    def factory(name, **kwargs):
        return _FakeProvider()

    monkeypatch.setattr("trading.ai_research.market_data.service.create_market_data_provider", factory)
    yield
    research_service._reset_semaphore()
    backtest_service._reset_semaphore()


def _fake_result(context_capture: dict):
    def _fake_run(instrument, as_of_date, *, market_data_context=None, **kwargs):
        context_capture["market_data_context"] = market_data_context
        return ResearchResult(
            instrument=instrument, as_of_date=as_of_date,
            report=ResearchReport(final_trade_decision="Rating: Hold", signal="Hold"),
            raw_state={},
        )
    return _fake_run


def test_india_research_carries_real_market_data_context_and_provenance(client, viewer_auth, monkeypatch):
    captured = {}
    monkeypatch.setattr("trading.ai_research.service.run_research", _fake_result(captured))

    submit = client.post(
        "/api/ai-research", headers=viewer_auth,
        json={"symbol": "NIFTY", "research_date": _REAL_TRADING_DAY, "market": "INDIA", "research_depth": "quick"},
    )
    assert submit.status_code == 200
    research_id = submit.json()["research_id"]

    status = None
    for _ in range(100):
        status = client.get(f"/api/ai-research/{research_id}/status", headers=viewer_auth).json()
        if status["status"] == "COMPLETED":
            break
        time.sleep(0.02)
    assert status["status"] == "COMPLETED"

    # The context actually handed to run_research() must contain real candle data.
    assert captured["market_data_context"] is not None
    assert "23320.00" in captured["market_data_context"] or "23320.0" in captured["market_data_context"]

    # Provenance is persisted and visible via the API (Section 19/29).
    assert status["market_data_provenance"] is not None
    assert status["market_data_provenance"]["provider"] == "icici_breeze"
    assert status["market_data_provenance"]["candle_count"] == 1

    result = client.get(f"/api/ai-research/{research_id}", headers=viewer_auth).json()
    # PARTIAL is the honest status: the fake provider only has data for
    # the research date itself, not the other real NSE trading days in
    # the 10-day lookback window -- it must not be reported as fully
    # AVAILABLE when most of the window is genuinely missing.
    assert result["market_data_provenance"]["status"] == "PARTIAL"
    assert result["market_data_provenance"]["candle_count"] == 1


def test_us_research_has_no_market_data_provenance(client, viewer_auth, monkeypatch):
    captured = {}
    monkeypatch.setattr("trading.ai_research.service.run_research", _fake_result(captured))

    yesterday = "2026-01-27"
    submit = client.post(
        "/api/ai-research", headers=viewer_auth,
        json={"symbol": "AAPL", "research_date": yesterday, "research_depth": "quick"},
    )
    research_id = submit.json()["research_id"]
    status = None
    for _ in range(100):
        status = client.get(f"/api/ai-research/{research_id}/status", headers=viewer_auth).json()
        if status["status"] == "COMPLETED":
            break
        time.sleep(0.02)
    assert status["status"] == "COMPLETED"
    assert captured["market_data_context"] is None
    assert status["market_data_provenance"] is None


def test_india_backtest_cell_uses_market_data_service(client, viewer_auth, monkeypatch):
    captured = {}
    monkeypatch.setattr("trading.ai_research.trading_agents_adapter.run_research", _fake_result(captured))
    monkeypatch.setattr("trading.ai_research.backtest.service.settle_pending_decisions", lambda *a, **k: None)

    submit = client.post(
        "/api/ai-research/backtests", headers=viewer_auth,
        json={
            "symbols": ["NIFTY"], "start_date": _REAL_TRADING_DAY, "end_date": _REAL_TRADING_DAY,
            "frequency": "daily", "market": "INDIA",
        },
    )
    assert submit.status_code == 200
    backtest_id = submit.json()["backtest_id"]

    status = None
    for _ in range(100):
        status = client.get(f"/api/ai-research/backtests/{backtest_id}/status", headers=viewer_auth).json()
        if status["status"] in ("COMPLETED", "FAILED"):
            break
        time.sleep(0.02)
    assert status["status"] == "COMPLETED"
    assert captured["market_data_context"] is not None
    assert "icici_breeze" in captured["market_data_context"]

    runs = client.get(f"/api/ai-research/backtests/{backtest_id}/runs", headers=viewer_auth).json()
    cell = runs["items"][0]
    drilldown = client.get(f"/api/ai-research/{cell['research_id']}", headers=viewer_auth).json()
    assert drilldown["market_data_provenance"]["provider"] == "icici_breeze"
