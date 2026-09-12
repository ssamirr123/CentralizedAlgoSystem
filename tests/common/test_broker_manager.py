"""trading/common/broker_manager.py -- account_id -> TradingAccount -> BrokerClient
resolution, both eager (pre-built client) and lazy (factory)."""
from __future__ import annotations

import pytest

from trading.common.broker_manager import BrokerManager, BrokerUnavailableError, UnknownAccountError
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.trading_account import ConnectionState, TradingAccount


def _account(account_id="PAPER_MAIN", enabled=True) -> TradingAccount:
    return TradingAccount(account_id=account_id, account_name="Paper", broker_id="paper", enabled=enabled)


def test_register_with_prebuilt_connected_client():
    manager = BrokerManager()
    paper = PaperBroker()
    paper.connect()

    manager.register_account(_account(), broker_client=paper)

    assert manager.get_account("PAPER_MAIN").connection_state == ConnectionState.CONNECTED
    assert manager.get_broker("PAPER_MAIN") is paper
    assert manager.is_available("PAPER_MAIN") is True


def test_register_with_prebuilt_but_not_yet_connected_client():
    manager = BrokerManager()
    paper = PaperBroker()  # not connected

    manager.register_account(_account(), broker_client=paper)

    assert manager.get_account("PAPER_MAIN").connection_state == ConnectionState.DISCONNECTED
    assert manager.is_available("PAPER_MAIN") is False


def test_unknown_account_raises():
    manager = BrokerManager()
    with pytest.raises(UnknownAccountError):
        manager.get_account("NO_SUCH_ACCOUNT")
    with pytest.raises(UnknownAccountError):
        manager.get_broker("NO_SUCH_ACCOUNT")


def test_lazy_factory_connects_on_first_use_and_is_cached():
    manager = BrokerManager()
    build_calls = []

    def factory():
        build_calls.append(1)
        return PaperBroker()

    manager.register_account(_account(), broker_factory=factory)
    assert manager.get_account("PAPER_MAIN").connection_state == ConnectionState.DISCONNECTED

    broker_first = manager.get_broker("PAPER_MAIN")
    assert len(build_calls) == 1
    assert broker_first.is_connected() is True
    assert manager.get_account("PAPER_MAIN").connection_state == ConnectionState.CONNECTED

    broker_second = manager.get_broker("PAPER_MAIN")
    assert broker_second is broker_first
    assert len(build_calls) == 1  # factory not called again


def test_disabled_account_reports_unavailable_even_if_connected():
    manager = BrokerManager()
    paper = PaperBroker()
    paper.connect()

    manager.register_account(_account(enabled=False), broker_client=paper)

    assert manager.is_available("PAPER_MAIN") is False
    # BrokerManager itself is mechanism-only -- resolving the broker for a
    # disabled account is still possible; policy enforcement (rejecting a
    # disabled account) is RiskManager/StrategyAssignment's job.
    assert manager.get_broker("PAPER_MAIN") is paper


def test_accounts_lists_every_registered_account():
    manager = BrokerManager()
    manager.register_account(_account("A"), broker_client=PaperBroker())
    manager.register_account(_account("B"), broker_client=PaperBroker())

    ids = {a.account_id for a in manager.accounts()}
    assert ids == {"A", "B"}


# --------------------------------------------------------------------------- #
# Broker-level (not account-level) availability -- Phase 7
# --------------------------------------------------------------------------- #
def test_broker_defaults_to_available_when_never_registered():
    manager = BrokerManager()
    assert manager.is_broker_available("dhan") is True
    manager.require_broker_available("dhan")  # must not raise


def test_broker_marked_unavailable_is_reported_correctly():
    manager = BrokerManager()
    manager.set_broker_availability("dhan", False, reason="adapter not implemented yet")

    assert manager.is_broker_available("dhan") is False
    with pytest.raises(BrokerUnavailableError, match="not implemented yet"):
        manager.require_broker_available("dhan")


def test_broker_availability_is_independent_per_broker_id():
    manager = BrokerManager()
    manager.set_broker_availability("dhan", False, reason="not implemented")

    assert manager.is_broker_available("angelone") is True
    manager.require_broker_available("angelone")  # must not raise


def test_broker_availability_can_be_re_enabled():
    manager = BrokerManager()
    manager.set_broker_availability("dhan", False)
    manager.set_broker_availability("dhan", True)

    assert manager.is_broker_available("dhan") is True
