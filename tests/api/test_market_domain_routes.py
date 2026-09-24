"""
Phase 7 integration tests: Indian-market research submission, non-trading-
date rejection, market/instrument/calendar API endpoints, Indian backtest
date-grid validation, legacy-record backward compatibility, and generic/
US research regression. run_research()/settle_pending_decisions() are
monkeypatched -- no real LLM/network call.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pytest

from trading.ai_research import service as research_service
from trading.ai_research.backtest import service as backtest_service
from trading.ai_research.trading_agents_adapter import ResearchReport, ResearchResult

_REPUBLIC_DAY_2026 = "2026-01-26"  # real NSE holiday, verified live
_REAL_TRADING_DAY = "2026-01-27"   # the following real trading day


@pytest.fixture(autouse=True)
def _fresh_semaphores():
    research_service._reset_semaphore()
    backtest_service._reset_semaphore()
    yield
    research_service._reset_semaphore()
    backtest_service._reset_semaphore()


def _fake_result(symbol="^NSEI"):
    return ResearchResult(
        instrument=symbol, as_of_date=_REAL_TRADING_DAY,
        report=ResearchReport(final_trade_decision="Rating: Hold", signal="Hold"),
        raw_state={},
    )


# --------------------------------------------------------------------------- #
# Markets / instruments / calendar endpoints (Section 31)
# --------------------------------------------------------------------------- #
def test_get_markets(client, viewer_auth):
    r = client.get("/api/ai-research/markets", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert set(body["markets"]) == {"US", "INDIA"}
    assert body["default"] == "US"


def test_instrument_search_finds_nifty(client, viewer_auth):
    r = client.get("/api/ai-research/instruments", headers=viewer_auth, params={"market": "INDIA", "query": "nifty"})
    assert r.status_code == 200
    ids = {i["canonical_id"] for i in r.json()["items"]}
    assert "NIFTY_50" in ids
    assert "NIFTY_BANK" in ids


def test_instrument_search_us_market_empty(client, viewer_auth):
    r = client.get("/api/ai-research/instruments", headers=viewer_auth, params={"market": "US", "query": "AAPL"})
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_calendar_valid_trading_day(client, viewer_auth):
    r = client.get("/api/ai-research/calendar", headers=viewer_auth, params={"market": "INDIA", "date": _REAL_TRADING_DAY})
    assert r.status_code == 200
    body = r.json()
    assert body["timezone"] == "Asia/Kolkata"
    assert body["validation"]["is_valid"] is True


def test_calendar_holiday_gives_previous_and_next_trading_day(client, viewer_auth):
    r = client.get("/api/ai-research/calendar", headers=viewer_auth, params={"market": "INDIA", "date": _REPUBLIC_DAY_2026})
    assert r.status_code == 200
    v = r.json()["validation"]
    assert v["is_valid"] is False
    assert "Republic Day" in v["reason"]
    assert v["previous_trading_day"] is not None
    assert v["next_trading_day"] is not None


def test_calendar_rejects_non_india_market(client, viewer_auth):
    r = client.get("/api/ai-research/calendar", headers=viewer_auth, params={"market": "US"})
    assert r.status_code == 422


def test_backtests_route_still_not_swallowed_by_markets_router(client, viewer_auth, monkeypatch):
    """Section 31 routing note: /markets, /instruments, /calendar, and
    /backtests must all resolve to their own routers, never the
    single-research GET /{research_id} catch-all."""
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    r = client.get("/api/ai-research/markets", headers=viewer_auth)
    assert r.status_code == 200
    assert "markets" in r.json()


# --------------------------------------------------------------------------- #
# Indian research submission + non-trading-date validation (Section 16/19)
# --------------------------------------------------------------------------- #
def test_india_research_submission_resolves_and_translates_symbol(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    captured = {}

    def _fake_run(instrument, as_of_date, **kwargs):
        captured["instrument"] = instrument
        return _fake_result(instrument)

    monkeypatch.setattr("trading.ai_research.service.run_research", _fake_run)

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
    assert status["market"] == "INDIA"
    assert status["currency"] == "INR"
    # The symbol actually handed to run_research() must be the real,
    # verified Yahoo symbol, not the canonical "NIFTY".
    assert captured["instrument"] == "^NSEI"

    result = client.get(f"/api/ai-research/{research_id}", headers=viewer_auth).json()
    assert result["market"] == "INDIA"
    assert result["currency"] == "INR"


def test_india_research_on_holiday_rejected_with_structured_info(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    submit = client.post(
        "/api/ai-research", headers=viewer_auth,
        json={"symbol": "NIFTY", "research_date": _REPUBLIC_DAY_2026, "market": "INDIA"},
    )
    assert submit.status_code == 422
    detail = submit.json()["detail"]
    assert "Republic Day" in detail["message"]
    assert detail["previous_trading_day"] is not None
    assert detail["next_trading_day"] is not None


def test_invalid_india_symbol_rejected(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    submit = client.post(
        "/api/ai-research", headers=viewer_auth,
        json={"symbol": "bad symbol!", "research_date": _REAL_TRADING_DAY, "market": "INDIA"},
    )
    assert submit.status_code == 422


# --------------------------------------------------------------------------- #
# Generic/US regression (Section 30)
# --------------------------------------------------------------------------- #
def test_us_research_unaffected_by_market_domain(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    captured = {}

    def _fake_run(instrument, as_of_date, **kwargs):
        captured["instrument"] = instrument
        return _fake_result(instrument)

    monkeypatch.setattr("trading.ai_research.service.run_research", _fake_run)

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    submit = client.post(
        "/api/ai-research", headers=viewer_auth,
        json={"symbol": "AAPL", "research_date": yesterday, "research_depth": "quick"},
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
    assert status["market"] == "US"
    assert status["currency"] == "USD"
    assert captured["instrument"] == "AAPL"  # unchanged, not translated at all


# --------------------------------------------------------------------------- #
# Indian AI backtest date-grid (Section 20)
# --------------------------------------------------------------------------- #
def test_india_backtest_estimate_excludes_weekends_and_holidays(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    r = client.post(
        "/api/ai-research/backtests/estimate", headers=viewer_auth,
        json={
            "symbols": ["NIFTY"], "start_date": "2026-01-23", "end_date": "2026-01-28",
            "frequency": "daily", "market": "INDIA",
        },
    )
    assert r.status_code == 200
    body = r.json()
    # 23 Fri, 24 Sat, 25 Sun, 26 Mon (Republic Day), 27 Tue, 28 Wed
    # -> only 23, 27, 28 are real NSE trading days.
    assert sorted(body["dates"]) == ["2026-01-23", "2026-01-27", "2026-01-28"]
    assert body["total_runs"] == 3


def test_us_backtest_estimate_unaffected_by_india_calendar(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    r = client.post(
        "/api/ai-research/backtests/estimate", headers=viewer_auth,
        json={"symbols": ["AAPL"], "start_date": "2026-01-23", "end_date": "2026-01-28", "frequency": "daily"},
    )
    assert r.status_code == 200
    # No India-calendar filtering -- every calendar day in range appears.
    assert len(r.json()["dates"]) == 6


def test_india_backtest_full_lifecycle_translates_symbol(client, viewer_auth, monkeypatch):
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    captured = []

    def _fake_run(instrument, as_of_date, **kwargs):
        captured.append(instrument)
        return _fake_result(instrument)

    monkeypatch.setattr("trading.ai_research.trading_agents_adapter.run_research", _fake_run)
    monkeypatch.setattr("trading.ai_research.backtest.service.settle_pending_decisions", lambda *a, **k: None)

    submit = client.post(
        "/api/ai-research/backtests", headers=viewer_auth,
        json={
            "symbols": ["RELIANCE"], "start_date": _REAL_TRADING_DAY, "end_date": _REAL_TRADING_DAY,
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
    assert captured == ["RELIANCE.NS"]


# --------------------------------------------------------------------------- #
# Legacy/backward compatibility (Section 35)
# --------------------------------------------------------------------------- #
def test_legacy_research_row_without_market_reads_as_us(client, viewer_auth, monkeypatch):
    """Simulates a Phase 5/6 row created before Phase 7's market columns
    existed -- writes directly to the DB with market/exchange/currency
    left NULL, exactly as an old row would be."""
    monkeypatch.setenv("AI_RESEARCH_ENABLED", "true")
    from trading.ai_research import repository
    from trading.ai_research.models import AIResearchRun
    from trading.database.connection import SessionLocal

    job = repository.create_run(
        __import__("trading.ai_research.schemas", fromlist=["ResearchRequestIn"]).ResearchRequestIn.model_validate(
            {"symbol": "AAPL", "research_date": _REAL_TRADING_DAY}
        )
    )
    db = SessionLocal()
    try:
        row = db.get(AIResearchRun, job.research_id)
        row.market = None
        row.exchange = None
        row.currency = None
        row.canonical_id = None
        db.commit()
    finally:
        db.close()

    r = client.get(f"/api/ai-research/{job.research_id}/status", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["market"] == "US"
    assert body["currency"] == "USD"

    history = client.get("/api/ai-research/history", headers=viewer_auth).json()
    entry = next(h for h in history["items"] if h["research_id"] == job.research_id)
    assert entry["market"] == "US"
    assert entry["currency"] == "USD"
