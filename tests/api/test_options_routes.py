"""Phase 9 -- Options Intelligence API tests (Sections 37/38/58/59)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trading.market_data.cache import LiveCache
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.schemas import IndexQuote, OptionChain, OptionChainRow, OptionQuote
from trading.market_data.service import MarketDataService, set_service
from trading.market_data.symbols import option_instrument

EXPIRY = date(2100, 9, 24)


@pytest.fixture
def market_with_options(db_session):
    cache = LiveCache(stale_seconds=60)
    cache.put(IndexQuote.build("NIFTY", ltp=25000.0, provider="icici_breeze"))
    for strike, c_ltp, p_ltp, c_oi, p_oi in ((24900, 220, 60, 1000, 2000), (25000, 150, 100, 5000, 4000), (25100, 90, 170, 3000, 6000)):
        cache.put(OptionQuote.build(underlying="NIFTY", expiry=EXPIRY, strike=strike, option_type="CE", ltp=c_ltp, oi=c_oi))
        cache.put(OptionQuote.build(underlying="NIFTY", expiry=EXPIRY, strike=strike, option_type="PE", ltp=p_ltp, oi=p_oi))

    master = InstrumentMaster()
    master.load(
        [option_instrument("NIFTY", EXPIRY, k, ot) for k in (24900, 25000, 25100) for ot in ("CE", "PE")],
        as_of=date(2100, 9, 1),
    )
    svc = MarketDataService(cache=cache, instrument_master=master, session_factory=lambda: None)
    set_service(svc)
    yield svc
    set_service(None)


def test_expiries_requires_auth(client, market_with_options):
    assert client.get("/api/options/expiries?underlying=NIFTY").status_code == 401


def test_expiries_returns_normalized_metadata(client, viewer_auth, market_with_options):
    r = client.get("/api/options/expiries?underlying=NIFTY", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert body[0]["expiry"] == EXPIRY.isoformat()
    assert "days_to_expiry_calendar" in body[0]
    assert "is_monthly" in body[0]


def test_unsupported_underlying_rejected(client, viewer_auth, market_with_options):
    r = client.get("/api/options/expiries?underlying=DOWJONES", headers=viewer_auth)
    assert r.status_code == 400


def test_chain_uses_live_cache_zero_provider_calls(client, viewer_auth, market_with_options):
    r = client.get(f"/api/options/chain?underlying=NIFTY&expiry={EXPIRY.isoformat()}&strike_window=1", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "LIVE_CACHE"
    assert body["provider_call_count"] == 0
    assert body["atm_strike"] == 25000
    strikes = [row["strike"] for row in body["rows"]]
    assert strikes == [24900, 25000, 25100]


def test_intelligence_endpoint_returns_full_structure(client, viewer_auth, market_with_options):
    r = client.get(f"/api/options/intelligence?underlying=NIFTY&expiry={EXPIRY.isoformat()}&strike_window=1", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["underlying"] == "NIFTY"
    assert body["atm_strike"] == 25000
    assert body["oi_pcr"] == pytest.approx((2000 + 4000 + 6000) / (1000 + 5000 + 3000), abs=1e-3)
    assert body["max_pain_strike"] is not None
    assert body["expected_move"] == pytest.approx(150 + 100)
    assert body["iv_rank_status"] == "NOT_AVAILABLE"  # no historical IV accumulated yet
    assert body["provenance"]["provider_call_count"] == 0
    assert body["provenance"]["calculation_version"]
    assert body["quality"] in ("COMPLETE", "PARTIAL", "STALE", "MISSING")


def test_no_strategy_or_order_endpoints_exist(client, viewer_auth, market_with_options):
    # Section 59: this API surface is read-only analytics. No verb suggests
    # order placement/strategy execution anywhere in the router.
    for path in ("/api/options/order", "/api/options/strategy", "/api/options/execute"):
        r = client.get(path, headers=viewer_auth)
        assert r.status_code == 404


def test_credentials_never_appear_in_intelligence_response(client, viewer_auth, market_with_options, monkeypatch):
    monkeypatch.setenv("BREEZE_API_KEY", "super-secret-key-xyz")
    monkeypatch.setenv("BREEZE_SECRET_KEY", "super-secret-secret-abc")
    r = client.get(f"/api/options/intelligence?underlying=NIFTY&expiry={EXPIRY.isoformat()}", headers=viewer_auth)
    text = r.text
    assert "super-secret-key-xyz" not in text
    assert "super-secret-secret-abc" not in text


def test_historical_as_of_never_hits_provider(client, viewer_auth, market_with_options, db_session):
    from trading.market_data.options_service import persist_option_chain_snapshot

    ts = datetime(2100, 9, 1, 10, 0, tzinfo=timezone.utc)
    rows = [OptionChainRow(
        strike=25000,
        call=OptionQuote.build(underlying="NIFTY", expiry=EXPIRY, strike=25000, option_type="CE", ltp=150, oi=5000, provider_timestamp=ts),
        put=OptionQuote.build(underlying="NIFTY", expiry=EXPIRY, strike=25000, option_type="PE", ltp=100, oi=4000, provider_timestamp=ts),
    )]
    chain = OptionChain(underlying="NIFTY", expiry=EXPIRY, spot=25000, atm_strike=25000, generated_at=ts, rows=rows, provider="test")
    persist_option_chain_snapshot(db_session, chain)

    from urllib.parse import quote

    r = client.get(
        f"/api/options/chain?underlying=NIFTY&expiry={EXPIRY.isoformat()}&as_of={quote(ts.isoformat())}",
        headers=viewer_auth,
    )
    assert r.status_code == 200
    assert r.json()["source"] == "TIMESCALEDB"
    assert r.json()["provider_call_count"] == 0
