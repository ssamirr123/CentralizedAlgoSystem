"""Tests for Zerodha Kite Connect market-data provider.

Zero network / no `kiteconnect` needed: a fake client and fake ticker are
injected via `client_factory` and `ticker_factory`.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

try:
    import pytest
except ImportError:
    class _PytestRaises:
        def __init__(self, expected_exc):
            self.expected_exc = expected_exc
            self.value = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                raise AssertionError(f"Expected {self.expected_exc.__name__} was not raised")
            if issubclass(exc_type, self.expected_exc):
                self.value = exc_val
                return True
            return False

    class _PytestShim:
        raises = _PytestRaises

    pytest = _PytestShim()

from trading.core.config import Settings
from trading.market_data.providers import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderDataError,
    ZerodhaKiteProvider,
    create_market_data_provider,
)
from trading.market_data.schemas import IndexQuote, OptionQuote
from trading.market_data.session import BreezeSessionManager, SessionState
from trading.market_data.symbols import index_instrument, option_instrument

_API_KEY = "test-kite-api-key"
_SECRET = "test-kite-api-secret"
_ACCESS_TOKEN = "test-kite-daily-access-token-999"

_MOCK_INDEX_QUOTE = {
    "NSE:NIFTY 50": {
        "instrument_token": 256265,
        "timestamp": "2026-09-05 15:29:59",
        "last_price": 25123.25,
        "net_change": 45.2,
        "ohlc": {
            "open": 25050.0,
            "high": 25180.5,
            "low": 25010.0,
            "close": 25078.05,
        },
        "volume": 1234567,
    }
}

_MOCK_OPTION_QUOTE = {
    123456: {
        "instrument_token": 123456,
        "timestamp": "2026-09-05 15:29:59",
        "last_price": 151.55,
        "ohlc": {
            "open": 140.0,
            "high": 160.0,
            "low": 138.0,
            "close": 145.0,
        },
        "volume": 54321,
        "oi": 1234500,
        "average_price": 149.2,
        "depth": {
            "buy": [{"price": 151.2, "quantity": 100, "orders": 2}],
            "sell": [{"price": 151.9, "quantity": 150, "orders": 3}],
        },
    }
}

_MOCK_HISTORICAL = [
    {
        "date": "2026-09-05 09:15:00",
        "open": 25050.0,
        "high": 25060.0,
        "low": 25040.0,
        "close": 25055.0,
        "volume": 1000,
        "oi": 50000,
    },
    {
        "date": "2026-09-05 09:16:00",
        "open": 25055.0,
        "high": 25070.0,
        "low": 25052.0,
        "close": 25068.0,
        "volume": 1200,
        "oi": 50500,
    },
]

_MOCK_INSTRUMENTS_NFO = [
    {
        "instrument_token": 10001,
        "tradingsymbol": "NIFTY2692425000CE",
        "name": "NIFTY",
        "expiry": "2026-09-24",
        "strike": 25000.0,
        "tick_size": 0.05,
        "lot_size": 75,
        "instrument_type": "CE",
        "segment": "NFO-OPT",
        "exchange": "NFO",
    },
    {
        "instrument_token": 10002,
        "tradingsymbol": "NIFTY2692425000PE",
        "name": "NIFTY",
        "expiry": "2026-09-24",
        "strike": 25000.0,
        "tick_size": 0.05,
        "lot_size": 75,
        "instrument_type": "PE",
        "segment": "NFO-OPT",
        "exchange": "NFO",
    },
    {
        "instrument_token": 10003,
        "tradingsymbol": "NIFTY2692425100CE",
        "name": "NIFTY",
        "expiry": "2026-09-24",
        "strike": 25100.0,
        "tick_size": 0.05,
        "lot_size": 75,
        "instrument_type": "CE",
        "segment": "NFO-OPT",
        "exchange": "NFO",
    },
    {
        "instrument_token": 10004,
        "tradingsymbol": "NIFTY2692425100PE",
        "name": "NIFTY",
        "expiry": "2026-09-24",
        "strike": 25100.0,
        "tick_size": 0.05,
        "lot_size": 75,
        "instrument_type": "PE",
        "segment": "NFO-OPT",
        "exchange": "NFO",
    },
]


class FakeKiteClient:
    def __init__(self, api_key: str, *, session_error: Exception | None = None, quote_error: Exception | None = None):
        self.api_key = api_key
        self.access_token: str | None = None
        self._session_error = session_error
        self._quote_error = quote_error
        self.historical_calls: list = []

    def set_access_token(self, token: str):
        self.access_token = token

    def profile(self):
        if self._session_error is not None:
            raise self._session_error
        return {"user_id": "AB1234", "user_name": "Test User"}

    def quote(self, instruments):
        if self._quote_error is not None:
            raise self._quote_error
        result = {}
        for inst in instruments:
            key = str(inst)
            if key in _MOCK_INDEX_QUOTE:
                result[key] = _MOCK_INDEX_QUOTE[key]
            elif isinstance(inst, int) and inst in _MOCK_OPTION_QUOTE:
                result[inst] = _MOCK_OPTION_QUOTE[inst]
            elif key in [str(k) for k in _MOCK_OPTION_QUOTE]:
                result[key] = _MOCK_OPTION_QUOTE[int(key)]
            elif key.startswith("NSE:"):
                # fallback for other index
                result[key] = {
                    "instrument_token": 99999,
                    "last_price": 25000.0,
                    "ohlc": {"open": 24900.0, "high": 25100.0, "low": 24800.0, "close": 24950.0},
                    "volume": 500000,
                    "timestamp": "2026-09-05 15:29:59",
                }
            else:
                result[key] = {
                    "instrument_token": int(key) if key.isdigit() else 11111,
                    "last_price": 100.0,
                    "ohlc": {"open": 90.0, "high": 110.0, "low": 85.0, "close": 95.0},
                    "volume": 10000,
                    "oi": 50000,
                    "average_price": 98.0,
                    "depth": {"buy": [{"price": 99.5}], "sell": [{"price": 100.5}]},
                }
        return result

    def historical_data(self, instrument_token, from_date, to_date, interval, continuous=False, oi=True):
        self.historical_calls.append((instrument_token, interval, from_date, to_date))
        return list(_MOCK_HISTORICAL)

    def instruments(self, exchange="NFO"):
        return list(_MOCK_INSTRUMENTS_NFO)


class FakeKiteTicker:
    def __init__(self, api_key: str, access_token: str):
        self.api_key = api_key
        self.access_token = access_token
        self.ws_connected = False
        self.subscribed: list[int] = []
        self.MODE_FULL = "full"
        self.on_ticks = None
        self.on_connect = None
        self.on_close = None
        self.on_error = None

    def connect(self, threaded=True):
        self.ws_connected = True
        if self.on_connect:
            self.on_connect(self, {})

    def close(self):
        self.ws_connected = False
        if self.on_close:
            self.on_close(self, 1000, "Normal closure")

    def subscribe(self, tokens):
        self.subscribed.extend(tokens)

    def unsubscribe(self, tokens):
        for t in tokens:
            if t in self.subscribed:
                self.subscribed.remove(t)

    def set_mode(self, mode, tokens):
        pass

    def emit(self, ticks: list[dict]):
        if self.on_ticks:
            self.on_ticks(self, ticks)


def _make_provider(*, session_error=None, quote_error=None):
    holder: dict[str, Any] = {}

    def client_factory(api_key: str):
        c = FakeKiteClient(api_key, session_error=session_error, quote_error=quote_error)
        holder["client"] = c
        return c

    def ticker_factory(api_key: str, access_token: str):
        t = FakeKiteTicker(api_key, access_token)
        holder["ticker"] = t
        return t

    provider = ZerodhaKiteProvider(
        api_key=_API_KEY,
        api_secret=_SECRET,
        session_token=_ACCESS_TOKEN,
        client_factory=client_factory,
        ticker_factory=ticker_factory,
    )
    return provider, holder


# --- Lifecycle / Auth tests --------------------------------------------------
def test_connect_requires_api_key_and_access_token():
    p = ZerodhaKiteProvider(api_key="", session_token="", client_factory=lambda k: FakeKiteClient(k))
    with pytest.raises(ProviderAuthError):
        p.connect()
    assert not p.is_connected()


def test_connect_success():
    p, holder = _make_provider()
    p.connect()
    assert p.is_connected()
    assert holder["client"].access_token == _ACCESS_TOKEN


def test_connect_session_failure_is_auth_error_and_never_leaks_token():
    class TokenException(Exception):
        pass

    p, _ = _make_provider(session_error=TokenException(f"Token {_ACCESS_TOKEN} invalid"))
    with pytest.raises(ProviderAuthError) as ei:
        p.connect()
    assert _ACCESS_TOKEN not in str(ei.value)
    assert not p.is_connected()


def test_disconnect():
    p, holder = _make_provider()
    p.connect()
    p.disconnect()
    assert not p.is_connected()


# --- Quotes tests ------------------------------------------------------------
def test_get_index_quote_nifty():
    p, _ = _make_provider()
    p.connect()
    q = p.get_index_quote(index_instrument("NIFTY"))
    assert isinstance(q, IndexQuote)
    assert q.symbol == "NIFTY"
    assert q.ltp == 25123.25
    assert q.open == 25050.0
    assert q.high == 25180.5
    assert q.low == 25010.0
    assert q.prev_close == 25078.05
    assert q.volume == 1234567
    assert q.provider == "zerodha"


def test_get_index_quote_rejects_non_index():
    p, _ = _make_provider()
    p.connect()
    opt = option_instrument("NIFTY", date(2026, 9, 24), 25000, "CE")
    with pytest.raises(ValueError):
        p.get_index_quote(opt)


def test_get_option_quote():
    p, _ = _make_provider()
    p.connect()
    inst = option_instrument(
        "NIFTY", date(2026, 9, 24), 25000, "CE", provider="zerodha", provider_token="123456"
    )
    q = p.get_option_quote(inst)
    assert isinstance(q, OptionQuote)
    assert q.underlying == "NIFTY"
    assert q.strike == 25000.0
    assert q.option_type == "CE"
    assert q.ltp == 151.55
    assert q.volume == 54321
    assert q.oi == 1234500
    assert q.bid == 151.2
    assert q.ask == 151.9
    assert q.vwap == 149.2


# --- Historical candles tests ------------------------------------------------
def test_get_historical_candles():
    p, holder = _make_provider()
    p.connect()
    nifty = index_instrument("NIFTY")
    candles = p.get_historical_candles(
        nifty,
        interval="1minute",
        start=datetime(2026, 9, 5, 9, 15, tzinfo=timezone.utc),
        end=datetime(2026, 9, 5, 9, 20, tzinfo=timezone.utc),
    )
    assert len(candles) == 2
    assert candles[0].symbol == "NIFTY"
    assert candles[0].interval == "1minute"
    assert candles[0].open == 25050.0
    assert candles[0].close == 25055.0
    assert candles[0].volume == 1000
    assert candles[0].oi == 50000
    # Check interval mapped to Kite "minute"
    assert holder["client"].historical_calls[0][1] == "minute"


# --- Instrument master & Option chain tests ----------------------------------
def test_get_option_instruments():
    p, _ = _make_provider()
    p.connect()
    instruments = p.get_option_instruments("NIFTY")
    assert len(instruments) == 4
    assert all(i.underlying == "NIFTY" for i in instruments)
    assert {i.strike for i in instruments} == {25000.0, 25100.0}
    assert {i.provider_token for i in instruments} == {"10001", "10002", "10003", "10004"}


def test_get_option_chain():
    p, _ = _make_provider()
    p.connect()
    chain = p.get_option_chain("NIFTY", date(2026, 9, 24))
    assert chain.underlying == "NIFTY"
    assert chain.expiry == date(2026, 9, 24)
    assert len(chain.rows) == 2
    assert chain.rows[0].strike == 25000.0
    assert chain.rows[1].strike == 25100.0


# --- Streaming WebSocket (KiteTicker) tests ----------------------------------
def test_subscribe_and_tick_handling():
    p, holder = _make_provider()
    p.connect()

    received_quotes = []

    def on_tick(q):
        received_quotes.append(q)

    nifty = index_instrument("NIFTY")
    opt = option_instrument(
        "NIFTY", date(2026, 9, 24), 25000, "CE", provider="zerodha", provider_token="10001"
    )

    p.subscribe([nifty, opt], on_tick)
    ticker = holder["ticker"]
    assert ticker.ws_connected
    assert 256265 in ticker.subscribed
    assert 10001 in ticker.subscribed

    # Emit index tick
    ticker.emit([{
        "instrument_token": 256265,
        "last_price": 25150.0,
        "volume_traded": 1500000,
        "ohlc": {"open": 25050.0, "high": 25160.0, "low": 25040.0, "close": 25078.0},
        "last_trade_time": "2026-09-05 15:30:00",
    }])

    # Emit option tick
    ticker.emit([{
        "instrument_token": 10001,
        "last_price": 160.0,
        "volume_traded": 60000,
        "oi": 1300000,
        "average_traded_price": 158.0,
        "ohlc": {"open": 140.0, "high": 165.0, "low": 138.0, "close": 145.0},
        "depth": {"buy": [{"price": 159.5}], "sell": [{"price": 160.5}]},
        "last_trade_time": "2026-09-05 15:30:00",
    }])

    assert len(received_quotes) == 2
    idx_q, opt_q = received_quotes[0], received_quotes[1]
    assert isinstance(idx_q, IndexQuote)
    assert idx_q.symbol == "NIFTY"
    assert idx_q.ltp == 25150.0

    assert isinstance(opt_q, OptionQuote)
    assert opt_q.underlying == "NIFTY"
    assert opt_q.strike == 25000.0
    assert opt_q.ltp == 160.0
    assert opt_q.oi == 1300000
    assert opt_q.bid == 159.5
    assert opt_q.ask == 160.5


def test_unsubscribe():
    p, holder = _make_provider()
    p.connect()
    nifty = index_instrument("NIFTY")
    p.subscribe([nifty], lambda q: None)
    ticker = holder["ticker"]
    assert 256265 in ticker.subscribed

    p.unsubscribe([nifty])
    assert 256265 not in ticker.subscribed


# --- Factory & Session Manager tests -----------------------------------------
def test_create_market_data_provider_zerodha():
    provider = create_market_data_provider("zerodha", api_key="k", api_secret="s", session_token="t")
    assert isinstance(provider, ZerodhaKiteProvider)
    assert provider.name == "zerodha"

    kite_provider = create_market_data_provider("kite", api_key="k", api_secret="s", session_token="t")
    assert isinstance(kite_provider, ZerodhaKiteProvider)


def test_session_manager_with_zerodha():
    settings = Settings(
        market_data_provider="zerodha",
        zerodha_api_key="my-key",
        zerodha_api_secret="my-secret",
        zerodha_access_token="my-token",
    )

    def factory(name, **kwargs):
        return ZerodhaKiteProvider(
            api_key=kwargs.get("api_key", ""),
            session_token=kwargs.get("session_token", ""),
            client_factory=lambda k: FakeKiteClient(k),
        )

    sm = BreezeSessionManager(settings=settings, provider_factory=factory)
    creds = sm.credentials()
    assert creds.api_key == "my-key"
    assert creds.session_token == "my-token"

    check = sm.check(publish=False)
    assert check.state == SessionState.VALID
    assert "Zerodha" in check.detail


# --- Auto Login tests --------------------------------------------------------
class MockResponse:
    def __init__(self, json_data, status_code=200, headers=None, url=""):
        self._json = json_data
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url

    def json(self):
        return self._json


class MockHttpSession:
    def __init__(self, *, login_success=True, twofa_success=True):
        self.login_success = login_success
        self.twofa_success = twofa_success
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, data or {}))
        if "api/login" in url:
            if self.login_success:
                return MockResponse({"status": "success", "data": {"request_id": "req-xyz-123"}})
            return MockResponse({"status": "error", "message": "Invalid password"})
        if "api/twofa" in url:
            if self.twofa_success:
                return MockResponse({"status": "success"})
            return MockResponse({"status": "error", "message": "Bad TOTP"})
        return MockResponse({})

    def get(self, url, headers=None, allow_redirects=True, timeout=None):
        self.gets.append(url)
        return MockResponse(
            {},
            status_code=302,
            headers={"Location": "https://127.0.0.1/?status=success&action=login&request_token=mock-req-token-555"},
        )


class MockKiteForAuth:
    def __init__(self, api_key: str, *, session_fail=False):
        self.api_key = api_key
        self.session_fail = session_fail
        self.access_token: str | None = None

    def login_url(self):
        return f"https://kite.zerodha.com/connect/login?api_key={self.api_key}&v=3"

    def set_access_token(self, token: str):
        self.access_token = token

    def generate_session(self, request_token: str, api_secret: str):
        if self.session_fail:
            raise RuntimeError("Invalid api_secret or expired request_token")
        assert request_token == "mock-req-token-555"
        assert api_secret == "test-secret"
        return {"access_token": "generated-access-token-999"}


def test_login_zerodha_headless_success():
    from trading.market_data.zerodha_auth import login_zerodha_headless

    http_session = MockHttpSession()
    kite = MockKiteForAuth("test-api-key")
    token = login_zerodha_headless(
        api_key="test-api-key",
        api_secret="test-secret",
        user_id="AB1234",
        password="MyPassword",
        totp_key="JBSWY3DPEHPK3PXP",
        session_factory=lambda: http_session,
        kite_factory=lambda k: kite,
    )
    assert token == "generated-access-token-999"
    assert kite.access_token == "generated-access-token-999"
    assert len(http_session.posts) == 2
    assert "api/login" in http_session.posts[0][0]
    assert "api/twofa" in http_session.posts[1][0]
    assert len(http_session.gets) >= 1


def test_login_zerodha_headless_missing_parameters_error():
    from trading.market_data.zerodha_auth import AutoLoginError, login_zerodha_headless

    with pytest.raises(AutoLoginError) as ei:
        login_zerodha_headless(
            api_key="",
            api_secret="",
            user_id="AB1234",
            password="",
            totp_key="",
        )
    msg = str(ei.value)
    assert "missing required parameter(s)" in msg
    assert "api_key" in msg
    assert "password" in msg


def test_login_zerodha_headless_step1_bad_password_error():
    from trading.market_data.zerodha_auth import AutoLoginError, login_zerodha_headless

    http_session = MockHttpSession(login_success=False)
    with pytest.raises(AutoLoginError) as ei:
        login_zerodha_headless(
            api_key="test-api-key",
            api_secret="test-secret",
            user_id="AB1234",
            password="WrongPassword",
            totp_key="JBSWY3DPEHPK3PXP",
            session_factory=lambda: http_session,
            kite_factory=lambda k: MockKiteForAuth(k),
        )
    msg = str(ei.value)
    assert "Step 1 failed" in msg
    assert "Invalid password" in msg
    assert "WrongPassword" not in msg  # Never leaks secret password


def test_login_zerodha_headless_step1_rate_limited():
    from trading.market_data.zerodha_auth import AutoLoginError, login_zerodha_headless

    class RateLimitedSession(MockHttpSession):
        def post(self, url, data=None, headers=None, timeout=None):
            return MockResponse({}, status_code=429)

    with pytest.raises(AutoLoginError) as ei:
        login_zerodha_headless(
            api_key="test-api-key",
            api_secret="test-secret",
            user_id="AB1234",
            password="MyPassword",
            totp_key="JBSWY3DPEHPK3PXP",
            session_factory=lambda: RateLimitedSession(),
            kite_factory=lambda k: MockKiteForAuth(k),
        )
    assert "429" in str(ei.value)


def test_login_zerodha_headless_step2_bad_totp_error():
    from trading.market_data.zerodha_auth import AutoLoginError, login_zerodha_headless

    http_session = MockHttpSession(login_success=True, twofa_success=False)
    with pytest.raises(AutoLoginError) as ei:
        login_zerodha_headless(
            api_key="test-api-key",
            api_secret="test-secret",
            user_id="AB1234",
            password="CorrectPassword",
            totp_key="JBSWY3DPEHPK3PXP",
            session_factory=lambda: http_session,
            kite_factory=lambda k: MockKiteForAuth(k),
        )
    msg = str(ei.value)
    assert "Step 2 failed" in msg
    assert "Bad TOTP" in msg


def test_login_zerodha_headless_step3_oauth_rejected():
    from trading.market_data.zerodha_auth import AutoLoginError, login_zerodha_headless

    class OAuthRejectedSession(MockHttpSession):
        def get(self, url, headers=None, allow_redirects=True, timeout=None):
            return MockResponse(
                {},
                status_code=302,
                headers={"Location": "https://127.0.0.1/?status=error&message=User+not+enabled+for+the+app"},
            )

    with pytest.raises(AutoLoginError) as ei:
        login_zerodha_headless(
            api_key="test-api-key",
            api_secret="test-secret",
            user_id="AB1234",
            password="CorrectPassword",
            totp_key="JBSWY3DPEHPK3PXP",
            session_factory=lambda: OAuthRejectedSession(),
            kite_factory=lambda k: MockKiteForAuth(k),
        )
    msg = str(ei.value)
    assert "Step 3 failed" in msg
    assert "User not enabled for the app" in msg


def test_login_zerodha_headless_step4_generate_session_error():
    from trading.market_data.zerodha_auth import AutoLoginError, login_zerodha_headless

    http_session = MockHttpSession()
    kite = MockKiteForAuth("test-api-key", session_fail=True)
    with pytest.raises(AutoLoginError) as ei:
        login_zerodha_headless(
            api_key="test-api-key",
            api_secret="wrong-secret",
            user_id="AB1234",
            password="Password",
            totp_key="JBSWY3DPEHPK3PXP",
            session_factory=lambda: http_session,
            kite_factory=lambda k: kite,
        )
    msg = str(ei.value)
    assert "Step 4 failed" in msg
    assert "generate_session" in msg


def test_login_zerodha_headless_handles_formatted_totp_key():
    from trading.market_data.zerodha_auth import login_zerodha_headless

    http_session = MockHttpSession()
    kite = MockKiteForAuth("test-api-key")
    # Formatted with spaces and lowercase
    token = login_zerodha_headless(
        api_key="test-api-key",
        api_secret="test-secret",
        user_id="AB1234",
        password="MyPassword",
        totp_key="jbsw-y3dp-ehpk-3pxp",
        session_factory=lambda: http_session,
        kite_factory=lambda k: kite,
    )
    assert token == "generated-access-token-999"


def test_login_zerodha_headless_uses_skip_session():
    from trading.market_data.zerodha_auth import login_zerodha_headless

    http_session = MockHttpSession()
    login_zerodha_headless(
        api_key="test-api-key",
        api_secret="test-secret",
        user_id="AB1234",
        password="MyPassword",
        totp_key="JBSWY3DPEHPK3PXP",
        session_factory=lambda: http_session,
        kite_factory=lambda k: MockKiteForAuth(k),
    )
    connect_gets = [u for u in http_session.gets if "/connect/login" in u]
    assert connect_gets and all("skip_session=true" in u for u in connect_gets)


def test_login_zerodha_headless_step3_html_page_gives_actionable_error():
    from trading.market_data.zerodha_auth import AutoLoginError, login_zerodha_headless

    class HtmlPageSession(MockHttpSession):
        def get(self, url, headers=None, allow_redirects=True, timeout=None):
            return MockResponse({}, status_code=200, url=url)

    with pytest.raises(AutoLoginError) as ei:
        login_zerodha_headless(
            api_key="test-api-key",
            api_secret="test-secret",
            user_id="AB1234",
            password="MyPassword",
            totp_key="JBSWY3DPEHPK3PXP",
            session_factory=lambda: HtmlPageSession(),
            kite_factory=lambda k: MockKiteForAuth(k),
        )
    msg = str(ei.value)
    assert "Step 3 failed" in msg
    assert "authorize" in msg
    assert "test-api-key" not in msg


def test_session_manager_auto_login_trigger():
    settings = Settings(
        market_data_provider="zerodha",
        zerodha_api_key="test-key",
        zerodha_api_secret="test-secret",
        zerodha_access_token="",  # Initially empty
        zerodha_user_id="AB1234",
        zerodha_password="MySecretPassword",
        zerodha_totp_key="JBSWY3DPEHPK3PXP",
    )

    def provider_factory(name, **kwargs):
        c = FakeKiteClient(kwargs.get("api_key", ""))
        c.set_access_token(kwargs.get("session_token", ""))
        return ZerodhaKiteProvider(
            api_key=kwargs.get("api_key", ""),
            session_token=kwargs.get("session_token", ""),
            client_factory=lambda k: c,
        )

    sm = BreezeSessionManager(settings=settings, provider_factory=provider_factory)
    assert sm.can_auto_login()

    # Mock login_zerodha_headless in zerodha_auth module
    import trading.market_data.zerodha_auth as za
    orig = za.login_zerodha_headless
    try:
        za.login_zerodha_headless = lambda **kw: "auto-generated-token-777"
        check = sm.check(publish=False)
        assert check.state == SessionState.VALID
        assert sm.credentials().session_token == "auto-generated-token-777"
    finally:
        za.login_zerodha_headless = orig

