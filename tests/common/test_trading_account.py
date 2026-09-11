"""trading/common/trading_account.py -- TradingAccount availability rules
and the secrecy guard on its repr()."""
from __future__ import annotations

from trading.common.brokers.paper_broker import PaperBroker
from trading.common.trading_account import ConnectionState, TradingAccount


def test_defaults_are_disconnected_but_enabled():
    account = TradingAccount(account_id="PAPER_MAIN", account_name="Paper", broker_id="paper")
    assert account.enabled is True
    assert account.connection_state == ConnectionState.DISCONNECTED
    assert account.is_available() is False


def test_is_available_requires_enabled_and_connected():
    account = TradingAccount(account_id="PAPER_MAIN", account_name="Paper", broker_id="paper")

    account.connection_state = ConnectionState.CONNECTED
    assert account.is_available() is True

    account.enabled = False
    assert account.is_available() is False

    account.enabled = True
    account.connection_state = ConnectionState.ERROR
    assert account.is_available() is False


def test_repr_never_exposes_the_broker_client():
    account = TradingAccount(account_id="PAPER_MAIN", account_name="Paper", broker_id="paper")
    account.broker_client = PaperBroker()

    text = repr(account)

    assert "PaperBroker" not in text
    assert "broker_client" not in text
    assert "PAPER_MAIN" in text
