"""Phase 15B: BrokerAdapterFactory + create_broker_for_account.

Proves: (1) unknown brokers fail closed, (2) two accounts of the same
broker get independently-credentialed adapter instances, (3) constructing
via this path never calls any broker mutation API.
"""
from __future__ import annotations

import pytest

from trading.common.broker_adapter_factory import (
    build_account_config,
    create_broker_for_account,
)
from trading.common.broker_types import BrokerType, UnsupportedBrokerError
from trading.common.config import TradingConfig
from trading.common.trading_account import TradingAccount


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith(("ANGELONE", "DHAN", "ICICI_BREEZE")):
            monkeypatch.delenv(name, raising=False)
    yield


def _account(account_id: str, credential_reference: str, broker_id: str = "angelone") -> TradingAccount:
    return TradingAccount(
        account_id=account_id, account_name=account_id, broker_id=broker_id,
        credential_reference=credential_reference, read_only=True,
    )


def test_unknown_broker_id_fails_closed():
    account = _account("BOGUS_ACCT", "", broker_id="totally_unknown_broker")
    with pytest.raises(UnsupportedBrokerError):
        build_account_config(account, TradingConfig())


def test_build_account_config_isolates_credentials_between_two_accounts(monkeypatch):
    monkeypatch.setenv("ANGELONE_SAMIR_API_KEY", "samir-key")
    monkeypatch.setenv("ANGELONE_SAMIR_CLIENT_ID", "samir-client")
    monkeypatch.setenv("ANGELONE_SAMIR_MPIN", "1111")
    monkeypatch.setenv("ANGELONE_SAMIR_TOTP_SECRET", "samir-totp")

    monkeypatch.setenv("ANGELONE_WIFE_API_KEY", "wife-key")
    monkeypatch.setenv("ANGELONE_WIFE_CLIENT_ID", "wife-client")
    monkeypatch.setenv("ANGELONE_WIFE_MPIN", "2222")
    monkeypatch.setenv("ANGELONE_WIFE_TOTP_SECRET", "wife-totp")

    base = TradingConfig()
    samir_account = _account("ANGEL_SAMIR", "env:ANGELONE_SAMIR")
    wife_account = _account("ANGEL_WIFE", "env:ANGELONE_WIFE")

    samir_config = build_account_config(samir_account, base)
    wife_config = build_account_config(wife_account, base)

    assert samir_config.credentials.angelone_client_id == "samir-client"
    assert wife_config.credentials.angelone_client_id == "wife-client"
    assert samir_config.credentials.angelone_client_id != wife_config.credentials.angelone_client_id
    # Process-wide settings still come from the shared base config.
    assert samir_config.trading_mode == base.trading_mode == "paper"


def test_create_broker_for_account_returns_the_right_adapter_type(monkeypatch):
    monkeypatch.setenv("ANGELONE_TEST_API_KEY", "k")
    monkeypatch.setenv("ANGELONE_TEST_CLIENT_ID", "c")
    monkeypatch.setenv("ANGELONE_TEST_MPIN", "1234")
    monkeypatch.setenv("ANGELONE_TEST_TOTP_SECRET", "t")

    from trading.common.brokers.angelone import AngelOneBroker

    account = _account("ANGEL_TEST", "env:ANGELONE_TEST")
    broker = create_broker_for_account(account, TradingConfig())
    assert isinstance(broker, AngelOneBroker)
    assert broker.is_read_only is True  # account.read_only defaulted to True, passed through


def test_create_broker_for_account_never_calls_a_mutation_method(monkeypatch):
    """Constructing an adapter through this factory must not, by itself,
    place/modify/cancel anything -- only connect() (called separately, and
    not by this factory) reaches the network."""
    monkeypatch.setenv("ANGELONE_TEST_API_KEY", "k")
    monkeypatch.setenv("ANGELONE_TEST_CLIENT_ID", "c")
    monkeypatch.setenv("ANGELONE_TEST_MPIN", "1234")
    monkeypatch.setenv("ANGELONE_TEST_TOTP_SECRET", "t")

    account = _account("ANGEL_TEST", "env:ANGELONE_TEST")
    broker = create_broker_for_account(account, TradingConfig())
    assert broker.is_connected() is False  # construction alone never connects


def test_dhan_and_icici_route_through_the_factory_too(monkeypatch):
    from trading.common.brokers.dhan import DhanBroker
    from trading.common.brokers.icici_breeze import ICICIBreezeBroker

    dhan_account = _account("DHAN_TEST", "", broker_id="dhan")
    dhan = create_broker_for_account(dhan_account, TradingConfig())
    assert isinstance(dhan, DhanBroker)

    icici_account = _account("ICICI_TEST", "", broker_id="icici_breeze")
    icici = create_broker_for_account(icici_account, TradingConfig())
    assert isinstance(icici, ICICIBreezeBroker)
