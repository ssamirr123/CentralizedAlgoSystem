"""Phase 2 -- ICICI Breeze provider.

Zero network / no `breeze-connect` needed: a fake client is injected via
`client_factory` (Phase 20's mock-Breeze seam).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from trading.market_data.providers import (
    ICICIBreezeProvider,
    ProviderAuthError,
    ProviderConnectionError,
    ProviderDataError,
    ProviderRateLimitError,
    create_market_data_provider,
)
from trading.market_data.schemas import IndexQuote, OptionQuote
from trading.market_data.symbols import index_instrument, option_instrument

_SECRET = "super-secret-value"
_SESSION = "TODAYS-SESSION-TOKEN-1234"

_INDEX_QUOTE = {
    "exchange_code": "NSE", "product_type": "cash", "stock_code": "NIFTY",
    "ltp": 25123.25, "ltt": "05-Sep-2026 15:29:59",
    "open": 25050.0, "high": 25180.5, "low": 25010.0, "previous_close": 25078.05,
    "total_quantity_traded": "0", "spot_price": "25123.25",
}

_CHAIN = [
    {"strike_price": "25000", "right": "Call", "ltp": "185.0", "best_bid_price": "184.5",
     "best_offer_price": "185.4", "open_interest": "900000", "oi_change": "5000",
     "total_quantity_traded": "12000", "previous_close": "170.0", "ltt": "05-Sep-2026 15:29:59"},
    {"strike_price": "25000", "right": "Put", "ltp": "60.0", "best_bid_price": "59.8",
     "best_offer_price": "60.3", "open_interest": "700000", "total_quantity_traded": "9000",
     "previous_close": "72.0"},
    {"strike_price": "25100", "right": "call", "ltp": "151.55", "open_interest": "1234500",
     "oi_change": "12000", "total_quantity_traded": "54321", "previous_close": "145.0"},
    {"strike_price": "25100", "right": "put", "ltp": "114.6", "open_interest": "980000",
     "total_quantity_traded": "34567", "previous_close": "120.0"},
    {"strike_price": "25200", "right": "Call", "ltp": "95.0", "open_interest": "500000"},
    {"strike_price": "25200", "right": "Put", "ltp": "160.0", "open_interest": "600000"},
]

_HIST = [
    {"datetime": "2026-09-05 09:15:00", "open": "25050", "high": "25060", "low": "25040",
     "close": "25055", "volume": "0", "open_interest": "0"},
    {"datetime": "2026-09-05 09:16:00", "open": "25055", "high": "25070", "low": "25052",
     "close": "25068", "volume": "0", "open_interest": "0"},
]


class FakeBreeze:
    """Mimics the slice of breeze_connect.BreezeConnect the provider uses."""

    def __init__(self, api_key, *, session_error=None, quote_error=None):
        self.api_key = api_key
        self._session_error = session_error
        self._quote_error = quote_error
        self.on_ticks = None
        self.ws_connected = False
        self.subscribed = []
        self.unsubscribed = []
        self.session_args = None

    def generate_session(self, api_secret, session_token):
        self.session_args = (api_secret, session_token)
        if self._session_error is not None:
            raise self._session_error

    def get_quotes(self, *, stock_code, exchange_code, product_type, expiry_date="", right="", strike_price=""):
        if self._quote_error is not None:
            return {"Success": None, "Status": 500, "Error": self._quote_error}
        if product_type == "options":
            return {"Success": [{
                "strike_price": strike_price, "right": right, "ltp": "151.55",
                "open_interest": "1234500", "oi_change": "12000",
                "total_quantity_traded": "54321", "previous_close": "145.0",
                "best_bid_price": "151.2", "best_offer_price": "151.9",
                "ltt": "05-Sep-2026 15:29:59",
            }], "Status": 200, "Error": None}
        q = dict(_INDEX_QUOTE)
        q["stock_code"] = stock_code
        return {"Success": [q], "Status": 200, "Error": None}

    def get_option_chain_quotes(self, **kwargs):
        return {"Success": list(_CHAIN), "Status": 200, "Error": None}

    def get_historical_data_v2(self, **kwargs):
        return {"Success": list(_HIST), "Status": 200, "Error": None}

    def ws_connect(self):
        self.ws_connected = True

    def ws_disconnect(self):
        self.ws_connected = False

    def subscribe_feeds(self, **kwargs):
        self.subscribed.append(kwargs)

    def unsubscribe_feeds(self, **kwargs):
        self.unsubscribed.append(kwargs)

    # test helper
    def emit(self, tick: dict):
        assert self.on_ticks is not None, "provider never registered on_ticks"
        self.on_ticks(tick)


def _provider(**fake_kwargs):
    holder = {}

    def factory(api_key):
        holder["client"] = FakeBreeze(api_key, **fake_kwargs)
        return holder["client"]

    p = ICICIBreezeProvider(
        api_key="k", api_secret=_SECRET, session_token=_SESSION, client_factory=factory
    )
    return p, holder


# --- lifecycle / auth -------------------------------------------------------
def test_connect_requires_all_three_credentials():
    p = ICICIBreezeProvider(api_key="", api_secret="", session_token="", client_factory=lambda k: FakeBreeze(k))
    with pytest.raises(ProviderAuthError):
        p.connect()
    assert not p.is_connected()


def test_connect_success():
    p, holder = _provider()
    p.connect()
    assert p.is_connected()
    assert holder["client"].session_args == (_SECRET, _SESSION)


def test_connect_session_failure_is_auth_error_and_never_leaks_token():
    p, _ = _provider(session_error=RuntimeError(f"bad token {_SESSION} rejected"))
    with pytest.raises(ProviderAuthError) as ei:
        p.connect()
    assert _SESSION not in str(ei.value)
    assert _SECRET not in str(ei.value)
    assert not p.is_connected()


def test_calls_before_connect_raise_connection_error():
    p, _ = _provider()
    with pytest.raises(ProviderConnectionError):
        p.get_index_quote(index_instrument("NIFTY"))


# --- index quote ----------------------------------------------------------
def test_get_index_quote_normalizes_and_derives_change():
    p, _ = _provider()
    p.connect()
    q = p.get_index_quote(index_instrument("NIFTY"))
    assert isinstance(q, IndexQuote)
    assert q.symbol == "NIFTY"
    assert q.ltp == 25123.25
    assert q.open == 25050.0 and q.high == 25180.5 and q.low == 25010.0
    assert q.prev_close == 25078.05
    assert q.change == round(25123.25 - 25078.05, 4)
    assert q.change_percent is not None
    assert q.provider == "icici_breeze"
    assert q.provider_timestamp == datetime(2026, 9, 5, 15, 29, 59, tzinfo=timezone.utc)


def test_get_index_quote_missing_field_is_none_not_invented():
    p, holder = _provider()
    p.connect()

    def only_ltp(**kw):
        return {"Success": [{"stock_code": kw["stock_code"], "ltp": 25000.0}], "Status": 200, "Error": None}

    holder["client"].get_quotes = only_ltp
    q = p.get_index_quote(index_instrument("BANKNIFTY"))
    assert q.ltp == 25000.0
    assert q.high is None and q.low is None and q.prev_close is None
    assert q.change is None and q.change_percent is None


def test_breeze_error_becomes_data_error():
    p, _ = _provider(quote_error="Invalid stock code")
    p.connect()
    with pytest.raises(ProviderDataError):
        p.get_index_quote(index_instrument("NIFTY"))


def test_breeze_rate_limit_error_is_typed():
    p, _ = _provider(quote_error="Rate limit exceeded, retry later")
    p.connect()
    with pytest.raises(ProviderRateLimitError):
        p.get_index_quote(index_instrument("NIFTY"))


# --- option chain -------------------------------------------------------
def test_get_option_chain_groups_by_strike_and_computes_atm():
    p, _ = _provider()
    p.connect()
    chain = p.get_option_chain("NIFTY", date(2026, 9, 3))
    assert chain.underlying == "NIFTY"
    assert chain.spot == 25123.25  # via the index quote lookup
    assert chain.atm_strike == 25100.0  # nearest listed strike to spot
    assert [r.strike for r in chain.rows] == [25000.0, 25100.0, 25200.0]

    atm_row = next(r for r in chain.rows if r.strike == 25100.0)
    assert isinstance(atm_row.call, OptionQuote) and isinstance(atm_row.put, OptionQuote)
    assert atm_row.call.ltp == 151.55
    assert atm_row.call.oi == 1234500
    assert atm_row.call.oi_change == 12000
    assert atm_row.put.ltp == 114.6
    # fields Breeze did not supply must be None, never fabricated
    assert atm_row.call.iv is None and atm_row.call.vwap is None
    assert atm_row.put.bid is None and atm_row.put.ask is None
    # last row has no previous_close -> change stays None
    far = next(r for r in chain.rows if r.strike == 25200.0)
    assert far.call.change is None


# --- historical -------------------------------------------------------
def test_get_historical_candles():
    p, _ = _provider()
    p.connect()
    candles = p.get_historical_candles(
        index_instrument("NIFTY"), "1minute",
        datetime(2026, 9, 5, 9, 15, tzinfo=timezone.utc),
        datetime(2026, 9, 5, 9, 20, tzinfo=timezone.utc),
    )
    assert len(candles) == 2
    c0 = candles[0]
    assert c0.symbol == "NIFTY" and c0.interval == "1minute"
    assert c0.timestamp == datetime(2026, 9, 5, 9, 15, 0, tzinfo=timezone.utc)
    assert (c0.open, c0.high, c0.low, c0.close) == (25050.0, 25060.0, 25040.0, 25055.0)


# --- streaming -------------------------------------------------------
def test_subscribe_registers_feed_and_normalizes_ticks():
    p, holder = _provider()
    p.connect()
    received = []
    p.subscribe([index_instrument("NIFTY"), index_instrument("SENSEX")], received.append)

    client = holder["client"]
    assert client.ws_connected is True
    assert len(client.subscribed) == 2
    assert {s["stock_code"] for s in client.subscribed} == {"NIFTY", "BSESEN"}

    client.emit({"stock_code": "NIFTY", "last": 25130.0, "previous_close": 25078.05,
                 "open": 25050.0, "ltt": "05-Sep-2026 15:29:59"})
    assert len(received) == 1
    tick = received[0]
    assert isinstance(tick, IndexQuote)
    assert tick.symbol == "NIFTY" and tick.ltp == 25130.0
    assert tick.change == round(25130.0 - 25078.05, 4)


def test_unsubscribe_and_disconnect():
    p, holder = _provider()
    p.connect()
    p.subscribe([index_instrument("NIFTY")], lambda q: None)
    p.unsubscribe([index_instrument("NIFTY")])
    assert len(holder["client"].unsubscribed) == 1
    p.disconnect()
    assert not p.is_connected()
    assert holder["client"].ws_connected is False


def test_bad_tick_does_not_raise():
    p, holder = _provider()
    p.connect()
    got = []
    p.subscribe([index_instrument("NIFTY")], got.append)
    holder["client"].emit("not-a-dict")          # ignored
    holder["client"].emit({"garbage": True})      # unresolvable symbol -> ignored
    assert got == []


# --- factory --------------------------------------------------------
def test_factory_builds_breeze_provider():
    p = create_market_data_provider("icici_breeze", api_key="k", api_secret="s", session_token="t")
    assert isinstance(p, ICICIBreezeProvider)
    assert create_market_data_provider("breeze", api_key="k", api_secret="s", session_token="t").name == "icici_breeze"


def test_factory_unknown_provider():
    with pytest.raises(ValueError):
        create_market_data_provider("bloomberg")
