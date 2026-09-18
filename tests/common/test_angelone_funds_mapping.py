"""Phase 15C.2.1: AngelOneBroker.get_funds() field-mapping test matrix.

Verified against a REAL rmsLimit() response (docs/phase-15c-2-1-funds-mapping-report.md):
    availablecash        -> available_cash    (decimal string, rupees)
    utiliseddebits       -> used_margin        (decimal string, rupees)
    availablelimitmargin -> available_margin   (decimal string, rupees; direct field)

Every scenario below must fail closed: a missing/null field defaults to
0.0 (matching the pre-existing "or 0" convention), but a MALFORMED
(non-numeric) value must raise BrokerConnectionError -- never silently
become a fabricated 0.0 that looks identical to a genuine zero balance.
"""
from __future__ import annotations

import pytest

from trading.common.broker import BrokerConnectionError
from trading.common.brokers.angelone import AngelOneBroker, FundsSnapshot
from trading.common.config import BrokerCredentials, TradingConfig


class _FakeSmartApi:
    def __init__(self, funds_response: dict | None = None, raise_on_rms: Exception | None = None):
        self.session_response = {"status": True, "data": {"refreshToken": "rt"}, "message": "SUCCESS"}
        self.funds_response = funds_response if funds_response is not None else {}
        self._raise_on_rms = raise_on_rms

    def generateSession(self, client_id, password, totp):
        return self.session_response

    def getfeedToken(self):
        return "ft"

    def rmsLimit(self):
        if self._raise_on_rms is not None:
            raise self._raise_on_rms
        return self.funds_response


def _config() -> TradingConfig:
    return TradingConfig(
        trading_mode="paper",
        credentials=BrokerCredentials(
            angelone_api_key="ak", angelone_client_id="C1", angelone_password="pw",
            angelone_totp_secret="JBSWY3DPEHPK3PXP",
        ),
    )


def _connected_broker(fake: _FakeSmartApi) -> AngelOneBroker:
    broker = AngelOneBroker(_config(), smart_api_factory=lambda k: fake, read_only=True)
    broker.connect()
    return broker


# --------------------------------------------------------------------------- #
# 1. Normal valid funds response -- all three fields, real field names
# --------------------------------------------------------------------------- #
def test_normal_valid_funds_response_maps_all_three_fields():
    fake = _FakeSmartApi({
        "status": True,
        "data": {"availablecash": "12345.50", "utiliseddebits": "678.25", "availablelimitmargin": "13000.00"},
    })
    funds = _connected_broker(fake).get_funds()
    assert funds == FundsSnapshot(available_cash=12345.50, used_margin=678.25, available_margin=13000.00)


# --------------------------------------------------------------------------- #
# 2. Zero funds response -- genuinely zero, reported as zero, not fabricated
# --------------------------------------------------------------------------- #
def test_zero_funds_response_is_reported_as_zero_not_fabricated():
    fake = _FakeSmartApi({
        "status": True,
        "data": {"availablecash": "0.0000", "utiliseddebits": "0.0000", "availablelimitmargin": "0.0000"},
    })
    funds = _connected_broker(fake).get_funds()
    assert funds == FundsSnapshot(available_cash=0.0, used_margin=0.0, available_margin=0.0)


# --------------------------------------------------------------------------- #
# 3. Missing funds field -- defaults to 0.0, matching the pre-existing convention
# --------------------------------------------------------------------------- #
def test_missing_funds_field_defaults_to_zero():
    fake = _FakeSmartApi({"status": True, "data": {"availablecash": "500.0"}})  # utiliseddebits/availablelimitmargin absent
    funds = _connected_broker(fake).get_funds()
    assert funds.used_margin == 0.0
    assert funds.available_margin == 0.0


# --------------------------------------------------------------------------- #
# 4. Malformed numeric value -- must fail closed, never become a fabricated 0.0
# --------------------------------------------------------------------------- #
def test_malformed_numeric_value_raises_broker_connection_error_not_silent_zero():
    fake = _FakeSmartApi({"status": True, "data": {"availablecash": "N/A", "utiliseddebits": "0", "availablelimitmargin": "0"}})
    with pytest.raises(BrokerConnectionError):
        _connected_broker(fake).get_funds()


# --------------------------------------------------------------------------- #
# 5. Unexpected/null value -- treated the same as missing (0.0), not an error
# --------------------------------------------------------------------------- #
def test_null_value_defaults_to_zero_like_a_missing_field():
    fake = _FakeSmartApi({
        "status": True,
        "data": {"availablecash": "100.0", "utiliseddebits": None, "availablelimitmargin": None},
    })
    funds = _connected_broker(fake).get_funds()
    assert funds.used_margin == 0.0
    assert funds.available_margin == 0.0


# --------------------------------------------------------------------------- #
# 6. Broker error response -- status:false must not be read as zero funds
# --------------------------------------------------------------------------- #
def test_broker_error_response_raises_not_treated_as_zero_funds():
    fake = _FakeSmartApi({"status": False, "message": "session expired", "data": None})
    with pytest.raises(BrokerConnectionError):
        _connected_broker(fake).get_funds()


def test_broker_exception_during_rms_limit_call_is_classified_not_swallowed():
    fake = _FakeSmartApi(raise_on_rms=RuntimeError("network blip"))
    with pytest.raises(BrokerConnectionError):
        _connected_broker(fake).get_funds()


# --------------------------------------------------------------------------- #
# 7/8/9/10: unit conversion + each normalized field individually
# --------------------------------------------------------------------------- #
def test_decimal_string_values_are_correctly_converted_to_float():
    """Angel's real rmsLimit() returns rupee amounts as decimal STRINGS
    (e.g. "0.0000"), not numbers -- confirmed against a real response."""
    fake = _FakeSmartApi({
        "status": True,
        "data": {"availablecash": "99999.9999", "utiliseddebits": "0.0001", "availablelimitmargin": "100000.0000"},
    })
    funds = _connected_broker(fake).get_funds()
    assert isinstance(funds.available_cash, float)
    assert funds.available_cash == pytest.approx(99999.9999)
    assert funds.used_margin == pytest.approx(0.0001)
    assert funds.available_margin == pytest.approx(100000.0)


def test_available_margin_is_a_direct_field_not_derived():
    """available_margin comes straight from availablelimitmargin -- it is
    NOT computed as available_cash - used_margin or any other formula."""
    fake = _FakeSmartApi({
        "status": True,
        "data": {"availablecash": "10.0", "utiliseddebits": "5.0", "availablelimitmargin": "999.0"},
    })
    funds = _connected_broker(fake).get_funds()
    assert funds.available_margin == 999.0  # not 10.0 - 5.0 = 5.0
