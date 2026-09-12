"""trading/common/trading_account.py -- TradingAccount availability rules
and the secrecy guard on its repr()."""
from __future__ import annotations

from trading.common.brokers.paper_broker import PaperBroker
from trading.common.trading_account import ConnectionState, ExecutionMode, TradingAccount


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


def test_execution_mode_defaults_to_paper():
    account = TradingAccount(account_id="PAPER_MAIN", account_name="Paper", broker_id="paper")
    assert account.execution_mode == ExecutionMode.PAPER


def test_execution_mode_accepts_plain_strings_and_coerces():
    account = TradingAccount(
        account_id="ANGEL_MAIN", account_name="Angel", broker_id="angelone", execution_mode="SHADOW",
    )
    assert account.execution_mode == ExecutionMode.SHADOW
    assert isinstance(account.execution_mode, ExecutionMode)


def test_environment_and_credential_reference_defaults():
    account = TradingAccount(account_id="PAPER_MAIN", account_name="Paper", broker_id="paper")
    assert account.environment == "production"
    assert account.credential_reference == ""


def test_environment_and_credential_reference_can_be_supplied():
    account = TradingAccount(
        account_id="ANGEL_MAIN", account_name="Angel", broker_id="angelone",
        environment="staging", credential_reference="env:ANGELONE_*",
    )
    assert account.environment == "staging"
    assert account.credential_reference == "env:ANGELONE_*"


def test_repr_includes_execution_mode_but_no_credential_reference_value():
    account = TradingAccount(
        account_id="ANGEL_MAIN", account_name="Angel", broker_id="angelone",
        execution_mode="LIVE", credential_reference="secretsmanager:angel-main-super-secret-arn",
    )
    text = repr(account)
    assert "LIVE" in text
    # credential_reference is a non-secret pointer, but repr() still
    # shouldn't gratuitously echo it -- keep the explicit field list short
    # and auditable rather than falling back to the dataclass default repr.
    assert "secretsmanager:angel-main-super-secret-arn" not in text
