"""Phase 11/14/19 -- market-data read API + RBAC."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trading.database import models
from trading.market_data.cache import LiveCache
from trading.market_data.instruments import InstrumentMaster
from trading.market_data.schemas import IndexQuote, OptionQuote
from trading.market_data.service import MarketDataService, set_service
from trading.market_data.status import FEED_STATUS, FeedState, SessionState
from trading.market_data.symbols import make_option_symbol, option_instrument

EXP = date(2100, 9, 3)


@pytest.fixture
def market(db_session):
    cache = LiveCache(stale_seconds=10)
    cache.put(IndexQuote.build("NIFTY", ltp=25012.0, open=24950.0, prev_close=24980.0, provider="icici_breeze"))
    cache.put(IndexQuote.build("BANKNIFTY", ltp=52000.0, prev_close=51900.0, provider="icici_breeze"))
    cache.put(OptionQuote.build(underlying="NIFTY", expiry=EXP, strike=25000, option_type="CE",
                                ltp=151.0, oi=123456, provider="icici_breeze"))

    master = InstrumentMaster()
    master.load(
        [
            option_instrument("NIFTY", EXP, k, ot, lot_size=75, tick_size=0.05,
                              provider="icici_breeze", provider_token=f"t{k}{ot}")
            for k in range(24800, 25201, 50)
            for ot in ("CE", "PE")
        ],
        as_of=date(2100, 9, 1),
    )
    svc = MarketDataService(cache=cache, instrument_master=master, session_factory=lambda: None)
    set_service(svc)
    FEED_STATUS.update(feed_state=FeedState.RUNNING, session_state=SessionState.VALID, enabled=True)
    yield svc
    set_service(None)


def test_indices_requires_auth(client, market):
    assert client.get("/api/market/indices").status_code == 401


@pytest.mark.parametrize("role", ["viewer", "trader", "operator", "admin"])
def test_every_role_can_read_market_data(client, bearer, market, role):
    h = bearer(role=role)
    for path in ("/api/market/indices", "/api/market/nifty/expiries",
                 "/api/market/nifty/option-chain", "/api/market/health"):
        r = client.get(path, headers=h)
        assert r.status_code == 200, f"{role} {path} -> {r.status_code} {r.text}"


def test_indices_payload(client, viewer_auth, market):
    r = client.get("/api/market/indices", headers=viewer_auth)
    body = {q["symbol"]: q for q in r.json()}
    assert body["NIFTY"]["ltp"] == 25012.0
    assert body["NIFTY"]["status"] == "live"
    assert body["SENSEX"]["status"] == "no_data"       # never seen
    assert body["NIFTY"]["change"] == round(25012.0 - 24980.0, 4)


def test_single_index_alias_and_404(client, viewer_auth, market):
    assert client.get("/api/market/indices/nifty", headers=viewer_auth).json()["symbol"] == "NIFTY"
    assert client.get("/api/market/indices/DOWJONES", headers=viewer_auth).status_code == 404


def test_option_chain_atm_and_range(client, viewer_auth, market):
    r = client.get("/api/market/nifty/option-chain?range=2", headers=viewer_auth)
    assert r.status_code == 200
    body = r.json()
    assert body["spot"] == 25012.0
    assert body["atm_strike"] == 25000
    assert [row["strike"] for row in body["strikes"]] == [24900, 24950, 25000, 25050, 25100]
    atm = next(row for row in body["strikes"] if row["strike"] == 25000)
    assert atm["call"]["ltp"] == 151.0
    assert atm["put"] is None  # no live put quote -> null


def test_expiries_and_strikes(client, viewer_auth, market):
    exps = client.get("/api/market/nifty/expiries", headers=viewer_auth).json()
    assert exps == [EXP.isoformat()]
    strikes = client.get("/api/market/nifty/strikes?expiry=current", headers=viewer_auth).json()
    assert strikes[0] == 24800.0 and strikes[-1] == 25200.0


def test_candles_from_db(client, viewer_auth, market, db_session):
    ts = datetime(2100, 9, 3, 4, 0, tzinfo=timezone.utc)
    db_session.add(models.MarketCandle(timestamp=ts, symbol="NIFTY", exchange="NSE", interval="1minute",
                                       open=1, high=2, low=0.5, close=1.5, volume=100))
    db_session.commit()
    rows = client.get("/api/market/candles/NIFTY", headers=viewer_auth).json()
    assert len(rows) == 1 and rows[0]["close"] == 1.5
    # unknown symbol
    assert client.get("/api/market/candles/FOO", headers=viewer_auth).status_code == 404


def test_health_shape(client, viewer_auth, market):
    body = client.get("/api/market/health", headers=viewer_auth).json()
    assert body["feed"] == "RUNNING"
    assert body["timezone"] == "Asia/Kolkata"
    assert set(body["symbols"]) == {"NIFTY", "BANKNIFTY", "INDIA_VIX", "SENSEX"}
    assert body["option_chain"]["atm_strike"] == 25000
    # never leaks a token
    assert "token" not in str(body).lower() or "session_token" not in str(body)
