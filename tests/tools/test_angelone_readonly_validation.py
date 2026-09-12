"""trading/tools/angelone_readonly_validation.py -- Phase 4 read-only
validation tool. Everything here runs against a FakeSmartApi double; no
real network call, no real credentials. conftest.py's session-wide
_no_network fixture is an additional, independent backstop: any attempt to
reach a real (non-loopback) host from this test file would raise loudly
rather than silently succeed or hang.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import trading.tools.angelone_readonly_validation as tool
from trading.common.broker import LiveTradingDisabledError, ReadOnlyModeError
from trading.common.brokers.angelone import AngelOneBroker
from trading.common.config import BrokerCredentials, TradingConfig


class FakeSmartApi:
    """Mirrors tests/common/test_angelone_broker.py's fake, but with the
    three mutating methods as MagicMocks so tests can assert_not_called()
    on them directly -- the core requirement of this test file."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session_response = {"status": True, "data": {"refreshToken": "rt"}, "message": "SUCCESS"}
        self.feed_token = "ft"
        self.ltp_response = {"status": True, "data": {"ltp": 125.5}}
        self.position_response = {"status": True, "data": []}
        self.orderbook_response = {"status": True, "data": []}
        self.funds_response = {"status": True, "data": {"availablecash": 1000.0, "utiliseddebits": 0.0}}
        self.profile_response = {"status": True, "data": {"clientcode": "C1", "name": "Test", "email": "t@example.com"}}
        self.placeOrder = MagicMock(name="placeOrder")
        self.modifyOrder = MagicMock(name="modifyOrder")
        self.cancelOrder = MagicMock(name="cancelOrder")

    def generateSession(self, client_id, password, totp):
        return self.session_response

    def getfeedToken(self):
        return self.feed_token

    def ltpData(self, exchange, tradingsymbol, symboltoken):
        return self.ltp_response

    def position(self):
        return self.position_response

    def orderBook(self):
        return self.orderbook_response

    def rmsLimit(self):
        return self.funds_response

    def getProfile(self, refresh_token):
        return self.profile_response


def _config(trading_mode="paper") -> TradingConfig:
    return TradingConfig(
        trading_mode=trading_mode,
        credentials=BrokerCredentials(
            angelone_api_key="ak", angelone_client_id="C1", angelone_password="pw",
            angelone_totp_secret="JBSWY3DPEHPK3PXP",  # syntactically valid base32 seed, not a real secret
        ),
    )


def _broker(fake=None, read_only=True, trading_mode="paper", instrument_resolver=None):
    fake = fake or FakeSmartApi("ak")
    broker = AngelOneBroker(
        _config(trading_mode),
        smart_api_factory=lambda api_key: fake,
        instrument_resolver=instrument_resolver or (lambda symbol: ("NFO", "99999")),
        read_only=read_only,
    )
    return broker, fake


SYMBOL = "NIFTY19MAY2623700CE"


# --------------------------------------------------------------------------- #
# Section 14/15: explicit safety / block tests
# --------------------------------------------------------------------------- #
def test_read_only_validation_never_places_order():
    broker, fake = _broker()

    report = tool.run_validation(SYMBOL, broker=broker)

    assert report.overall == "PASS"
    fake.placeOrder.assert_not_called()
    fake.modifyOrder.assert_not_called()
    fake.cancelOrder.assert_not_called()


def test_place_order_blocked_in_read_only_mode():
    from trading.common.broker import OrderSide, OrderType

    broker, fake = _broker(trading_mode="live")
    broker.connect()

    with pytest.raises(ReadOnlyModeError):
        broker.place_order(SYMBOL, OrderSide.BUY, 50, OrderType.MARKET)
    fake.placeOrder.assert_not_called()


def test_modify_order_blocked_in_read_only_mode():
    broker, fake = _broker()
    broker.connect()

    with pytest.raises(ReadOnlyModeError):
        broker.modify_order("SOME-ID", 50, 100.0)
    fake.modifyOrder.assert_not_called()


def test_cancel_order_blocked_in_read_only_mode():
    broker, fake = _broker(trading_mode="live")
    broker.connect()

    with pytest.raises(ReadOnlyModeError):
        broker.cancel_order("SOME-ID")
    fake.cancelOrder.assert_not_called()


def test_read_only_check_happens_before_live_trading_check():
    """Read-only must be checked FIRST -- proves it raises ReadOnlyModeError,
    not LiveTradingDisabledError, even though both conditions would block."""
    from trading.common.broker import OrderSide, OrderType

    broker, _fake = _broker(trading_mode="paper", read_only=True)  # both would block

    with pytest.raises(ReadOnlyModeError):
        broker.place_order(SYMBOL, OrderSide.BUY, 50, OrderType.MARKET)


# --------------------------------------------------------------------------- #
# Section 16: environment safety
# --------------------------------------------------------------------------- #
def test_trading_mode_live_never_disables_read_only(monkeypatch):
    from trading.common.broker import OrderSide, OrderType

    monkeypatch.setenv("ANGEL_READ_ONLY", "true")
    monkeypatch.setenv("TRADING_MODE", "live")
    broker, fake = _broker(trading_mode="live", read_only=None)  # picked up from env

    assert broker.is_read_only is True
    broker.connect()
    with pytest.raises(ReadOnlyModeError):
        broker.place_order(SYMBOL, OrderSide.SELL, 50, OrderType.LIMIT, limit_price=100.0)
    fake.placeOrder.assert_not_called()


def test_read_only_defaults_off_without_env_or_explicit_flag(monkeypatch):
    """Sanity check: read-only is opt-in, not accidentally always-on --
    confirms Phase 2/3 behavior (LiveTradingDisabledError, not
    ReadOnlyModeError) is unaffected when nobody asks for read-only."""
    from trading.common.broker import OrderSide, OrderType

    monkeypatch.delenv("ANGEL_READ_ONLY", raising=False)
    broker, _fake = _broker(trading_mode="paper", read_only=None)

    assert broker.is_read_only is False
    broker.connect()
    with pytest.raises(LiveTradingDisabledError):
        broker.place_order(SYMBOL, OrderSide.SELL, 50, OrderType.MARKET)


# --------------------------------------------------------------------------- #
# Read-only allowed operations
# --------------------------------------------------------------------------- #
def test_read_operations_are_all_allowed_in_read_only_mode():
    broker, _fake = _broker()
    broker.connect()

    broker.get_quote(SYMBOL)
    broker.get_positions()
    broker.get_funds()
    broker.get_order_book()
    broker.get_open_orders()
    broker.resolve_instrument(SYMBOL)
    broker.get_account_info()
    # No exception raised by any of the above -- that IS the assertion.


# --------------------------------------------------------------------------- #
# Full validation flow
# --------------------------------------------------------------------------- #
def test_full_validation_all_checks_pass():
    broker, _fake = _broker()

    report = tool.run_validation(SYMBOL, broker=broker)

    names = [r.name for r in report.results]
    assert names == [
        "Authentication", "Instrument Resolution", "Instrument Comparison",
        "Quote/LTP", "Funds", "Positions", "Order Book", "Open Orders",
    ]
    assert all(r.status == "PASS" for r in report.results if r.name != "Instrument Comparison")
    assert report.results[2].status == "SKIPPED"  # no --strike-suffix supplied
    assert report.overall == "PASS"


def test_authentication_failure_skips_everything():
    fake = FakeSmartApi("ak")
    fake.session_response = {"status": False, "message": "Invalid TOTP"}
    broker, _ = _broker(fake=fake)

    report = tool.run_validation(SYMBOL, broker=broker)

    assert report.results[0].name == "Authentication"
    assert report.results[0].status == "FAIL"
    assert len(report.results) == 1  # nothing else even attempted
    assert report.overall == "FAIL"


def test_quote_failure_skips_downstream_checks_only():
    fake = FakeSmartApi("ak")
    fake.ltp_response = {"status": False, "message": "symbol not found"}
    broker, _ = _broker(fake=fake)

    report = tool.run_validation(SYMBOL, broker=broker)

    by_name = {r.name: r.status for r in report.results}
    assert by_name["Authentication"] == "PASS"
    assert by_name["Instrument Resolution"] == "PASS"
    assert by_name["Quote/LTP"] == "FAIL"
    assert by_name["Funds"] == "SKIPPED"
    assert by_name["Positions"] == "SKIPPED"
    assert by_name["Order Book"] == "SKIPPED"
    assert by_name["Open Orders"] == "SKIPPED"
    assert report.overall == "FAIL"


def test_instrument_resolution_failure_skips_downstream_checks():
    def _boom(symbol):
        raise ValueError("AngelOneBroker: symbol not found in the NFO scrip master")

    broker, _fake = _broker(instrument_resolver=_boom)

    report = tool.run_validation(SYMBOL, broker=broker)

    by_name = {r.name: r.status for r in report.results}
    assert by_name["Instrument Resolution"] == "FAIL"
    assert by_name["Quote/LTP"] == "SKIPPED"
    assert report.overall == "FAIL"


def test_instrument_comparison_match(monkeypatch):
    monkeypatch.setattr(tool, "_resolve_via_token_file", lambda strike_suffix: (SYMBOL, "99999"))
    broker, _fake = _broker()

    report = tool.run_validation(SYMBOL, strike_suffix="23700CE", broker=broker)

    comparison = next(r for r in report.results if r.name == "Instrument Comparison")
    assert comparison.status == "PASS"
    assert "MATCH" in comparison.detail
    assert report.overall == "PASS"


def test_instrument_comparison_mismatch_stops_further_validation(monkeypatch):
    monkeypatch.setattr(tool, "_resolve_via_token_file", lambda strike_suffix: (SYMBOL, "WRONG-TOKEN"))
    broker, _fake = _broker()

    report = tool.run_validation(SYMBOL, strike_suffix="23700CE", broker=broker)

    by_name = {r.name: r.status for r in report.results}
    assert by_name["Instrument Comparison"] == "FAIL"
    assert "MISMATCH" in next(r.detail for r in report.results if r.name == "Instrument Comparison")
    assert by_name["Quote/LTP"] == "SKIPPED"
    assert report.overall == "FAIL"


def test_instrument_comparison_unavailable_is_skipped_not_failed(monkeypatch):
    def _boom(strike_suffix):
        raise FileNotFoundError("nifty_token.csv not found")

    monkeypatch.setattr(tool, "_resolve_via_token_file", _boom)
    broker, _fake = _broker()

    report = tool.run_validation(SYMBOL, strike_suffix="23700CE", broker=broker)

    comparison = next(r for r in report.results if r.name == "Instrument Comparison")
    assert comparison.status == "SKIPPED"
    # Unlike a real mismatch, an unavailable comparison must NOT stop the rest.
    by_name = {r.name: r.status for r in report.results}
    assert by_name["Quote/LTP"] == "PASS"
    assert report.overall == "PASS"


# --------------------------------------------------------------------------- #
# No credential leakage
# --------------------------------------------------------------------------- #
FORBIDDEN = ("api_key", "apikey", "client_secret", "password", "mpin", "totp", "jwt",
             "refresh_token", "feed_token", "auth_token", "session_token")


def test_sanitize_error_withholds_forbidden_content():
    text = tool._sanitize_error(RuntimeError("Invalid password: pw123"))
    assert "pw123" not in text
    assert "password" not in text.lower()


def test_sanitize_error_passes_through_safe_messages():
    text = tool._sanitize_error(RuntimeError("symbol not found in scrip master"))
    assert "symbol not found" in text


def test_no_credential_leakage_in_summary_output(capsys):
    fake = FakeSmartApi("ak")
    fake.session_response = {"status": False, "message": "Invalid TOTP for client secret abc123"}
    broker, _ = _broker(fake=fake)

    report = tool.run_validation(SYMBOL, broker=broker)
    tool._print_summary(report)

    out = capsys.readouterr().out.lower()
    for marker in FORBIDDEN:
        assert marker not in out
    assert "abc123" not in out


# --------------------------------------------------------------------------- #
# CLI entrypoint
# --------------------------------------------------------------------------- #
def test_main_reports_not_run_when_credentials_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(tool, "_load_dotenv_if_present", lambda: None)  # never touch a real trading/.env
    for var in ("ANGELONE_API_KEY", "ANGELONE_CLIENT_ID", "ANGELONE_PASSWORD", "ANGELONE_TOTP_SECRET"):
        monkeypatch.delenv(var, raising=False)

    code = tool.main(["--symbol", SYMBOL])

    out = capsys.readouterr().out
    assert "Real Angel session validation: NOT RUN" in out
    assert "credentials unavailable" in out
    assert code == 0


def test_main_requires_confirm_flag_when_credentials_present(monkeypatch, capsys):
    monkeypatch.setattr(tool, "_load_dotenv_if_present", lambda: None)
    monkeypatch.setenv("ANGELONE_API_KEY", "ak")
    monkeypatch.setenv("ANGELONE_CLIENT_ID", "C1")
    monkeypatch.setenv("ANGELONE_PASSWORD", "pw")
    monkeypatch.setenv("ANGELONE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

    code = tool.main(["--symbol", SYMBOL])  # no --read-only-confirm

    out = capsys.readouterr().out
    assert "--read-only-confirm" in out
    assert code == 1


def test_main_with_confirm_never_reaches_a_real_network(monkeypatch, capsys):
    """Even with --read-only-confirm and (fake) credentials present, main()
    must fail gracefully rather than crash -- conftest.py's _no_network
    fixture blocks the real HTTP call generateSession() would attempt,
    proving this path can never actually reach Angel One in this test."""
    monkeypatch.setattr(tool, "_load_dotenv_if_present", lambda: None)
    monkeypatch.setenv("ANGELONE_API_KEY", "ak")
    monkeypatch.setenv("ANGELONE_CLIENT_ID", "C1")
    monkeypatch.setenv("ANGELONE_PASSWORD", "pw")
    monkeypatch.setenv("ANGELONE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")

    code = tool.main(["--symbol", SYMBOL, "--read-only-confirm"])

    out = capsys.readouterr().out
    assert "Authentication" in out
    assert "FAIL" in out  # blocked by the network guard, reported as a sanitized failure
    assert code == 1


def test_main_banner_always_shows_read_only_enabled(monkeypatch, capsys):
    monkeypatch.setattr(tool, "_load_dotenv_if_present", lambda: None)
    for var in ("ANGELONE_API_KEY", "ANGELONE_CLIENT_ID", "ANGELONE_PASSWORD", "ANGELONE_TOTP_SECRET"):
        monkeypatch.delenv(var, raising=False)

    tool.main(["--symbol", SYMBOL])

    out = capsys.readouterr().out
    assert "READ ONLY: ENABLED" in out
    assert "Order placement: BLOCKED" in out
    assert "Order modification: BLOCKED" in out
    assert "Order cancellation: BLOCKED" in out
