"""trading/validation/angel_readonly.py -- Phase 5A validation tool.
Entirely offline: FakeSmartApi double, no real network, no real credentials.
conftest.py's session-wide _no_network fixture is an additional backstop.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import trading.validation.angel_readonly as tool
from trading.common.brokers.angelone import AngelOneBroker
from trading.common.config import BrokerCredentials, TradingConfig


class FakeSmartApi:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session_response = {"status": True, "data": {"refreshToken": "rt"}, "message": "SUCCESS"}
        self.feed_token = "ft"
        self.ltp_response = {"status": True, "data": {"ltp": 24800.5}}
        self.position_response = {"status": True, "data": []}
        self.orderbook_response = {"status": True, "data": []}
        self.funds_response = {"status": True, "data": {"availablecash": 1000.0, "utiliseddebits": 0.0}}
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


def _config() -> TradingConfig:
    return TradingConfig(
        trading_mode="paper",
        credentials=BrokerCredentials(
            angelone_api_key="ak", angelone_client_id="C1", angelone_password="pw",
            angelone_totp_secret="JBSWY3DPEHPK3PXP",
        ),
    )


def _resolver(symbol: str):
    if "DOES-NOT-EXIST" in symbol:
        raise ValueError(f"AngelOneBroker: symbol '{symbol}' not found in the NFO scrip master")
    return ("NFO", "99999")


def _broker(fake=None):
    fake = fake or FakeSmartApi("ak")
    broker = AngelOneBroker(
        _config(),
        smart_api_factory=lambda api_key: fake,
        instrument_resolver=_resolver,
        read_only=True,
    )
    return broker, fake


@pytest.fixture(autouse=True)
def _no_real_scrip_master(monkeypatch):
    """Every test must avoid the real pandas.read_json network call --
    monkeypatch the option-picking helper to a fixed, offline value."""
    monkeypatch.setattr(tool, "_pick_current_nifty_ce_symbol", lambda spot: ("NIFTY25SEP2624800CE", "2026-09-25"))


def test_full_validation_all_mandatory_checks_pass():
    broker, fake = _broker()
    report = tool.run_validation(config=_config(), broker=broker)

    for name in report.mandatory_checks:
        assert report.status_of(name) == "PASS", f"{name} failed: {report.results}"
    assert report.phase_status == "PASS"


def test_mutation_apis_are_never_actually_reached():
    broker, fake = _broker()
    tool.run_validation(config=_config(), broker=broker)

    fake.placeOrder.assert_not_called()
    fake.modifyOrder.assert_not_called()
    fake.cancelOrder.assert_not_called()


def test_mutation_safety_check_reports_blocked():
    broker, _fake = _broker()
    report = tool.run_validation(config=_config(), broker=broker)

    mutation = next(r for r in report.results if r.name == "MUTATION SAFETY")
    assert mutation.status == "PASS"
    assert "place_order=BLOCKED" in mutation.detail
    assert "modify_order=BLOCKED" in mutation.detail
    assert "cancel_order=BLOCKED" in mutation.detail


def test_credentials_unavailable_blocks_the_phase():
    config = TradingConfig(credentials=BrokerCredentials())  # nothing set

    report = tool.run_validation(config=config)

    assert report.phase_status == "BLOCKED"
    for name in report.mandatory_checks:
        assert report.status_of(name) == "FAIL"


def test_authentication_failure_blocks_all_mandatory_checks():
    fake = FakeSmartApi("ak")
    fake.session_response = {"status": False, "message": "Invalid TOTP"}
    broker, _ = _broker(fake=fake)

    report = tool.run_validation(config=_config(), broker=broker)

    assert report.status_of("AUTHENTICATION") == "FAIL"
    assert report.phase_status == "BLOCKED"
    for name in ("FUNDS", "NIFTY INSTRUMENT", "OPTION INSTRUMENT", "LTP", "POSITIONS", "ORDERS"):
        assert report.status_of(name) == "FAIL"


def test_authentication_failure_still_verifies_mutation_safety():
    fake = FakeSmartApi("ak")
    fake.session_response = {"status": False, "message": "Invalid TOTP"}
    broker, _ = _broker(fake=fake)

    report = tool.run_validation(config=_config(), broker=broker)

    mutation = next(r for r in report.results if r.name == "MUTATION SAFETY")
    assert mutation.status == "PASS"


def test_funds_failure_does_not_block_other_checks():
    fake = FakeSmartApi("ak")
    fake.funds_response = {"status": False, "message": "session expired"}
    broker, _ = _broker(fake=fake)

    report = tool.run_validation(config=_config(), broker=broker)

    assert report.status_of("FUNDS") == "FAIL"
    assert report.status_of("NIFTY INSTRUMENT") == "PASS"
    assert report.status_of("LTP") == "PASS"
    assert report.phase_status == "BLOCKED"  # FUNDS is mandatory


def test_nifty_index_resolves_via_the_special_case_not_the_default_resolver():
    broker, fake = _broker()

    report = tool.run_validation(config=_config(), broker=broker)

    nifty_check = next(r for r in report.results if r.name == "NIFTY INSTRUMENT")
    assert nifty_check.status == "PASS"
    assert "exchange=NSE" in nifty_check.detail


def test_option_instrument_uses_the_resolved_current_contract():
    broker, _fake = _broker()

    report = tool.run_validation(config=_config(), broker=broker)

    option_check = next(r for r in report.results if r.name == "OPTION INSTRUMENT")
    assert option_check.status == "PASS"
    assert "NIFTY25SEP2624800CE" in option_check.detail


def test_error_handling_check_catches_an_invalid_symbol_cleanly():
    broker, _fake = _broker()

    report = tool.run_validation(config=_config(), broker=broker)

    error_check = next(r for r in report.results if r.name == "ERROR HANDLING")
    assert error_check.status == "PASS"


def test_no_credential_leakage_in_report_details():
    fake = FakeSmartApi("ak")
    fake.session_response = {"status": False, "message": "Invalid TOTP for password abc123"}
    broker, _ = _broker(fake=fake)

    report = tool.run_validation(config=_config(), broker=broker)

    all_text = " ".join(r.detail.lower() for r in report.results)
    for marker in ("password", "totp", "abc123"):
        assert marker not in all_text


def test_main_reports_not_run_when_credentials_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(tool, "_load_dotenv_if_present", lambda: None)
    for var in ("ANGELONE_API_KEY", "ANGELONE_CLIENT_ID", "ANGELONE_PASSWORD", "ANGELONE_MPIN", "ANGELONE_TOTP_SECRET"):
        monkeypatch.delenv(var, raising=False)

    code = tool.main([])

    out = capsys.readouterr().out
    assert "PHASE 5A = BLOCKED" in out
    assert code == 1


def test_main_banner_confirms_order_apis_disabled(monkeypatch, capsys):
    monkeypatch.setattr(tool, "_load_dotenv_if_present", lambda: None)
    for var in ("ANGELONE_API_KEY", "ANGELONE_CLIENT_ID", "ANGELONE_PASSWORD", "ANGELONE_MPIN", "ANGELONE_TOTP_SECRET"):
        monkeypatch.delenv(var, raising=False)

    tool.main([])

    out = capsys.readouterr().out
    assert "ORDER PLACEMENT: DISABLED" in out
    assert "ORDER MODIFICATION: DISABLED" in out
    assert "ORDER CANCELLATION: DISABLED" in out
