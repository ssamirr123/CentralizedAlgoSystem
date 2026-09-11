"""trading/common/strategy_assignment.py -- strategy_id -> account_id,
never strategy_id -> broker."""
from __future__ import annotations

import pytest

from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.strategy_assignment import (
    InvalidAssignmentError,
    StrategyAssignment,
    UnknownAssignmentError,
)
from trading.common.trading_account import TradingAccount


def _manager_with_accounts() -> BrokerManager:
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id="PAPER_MAIN", account_name="Paper", broker_id="paper", enabled=True),
        broker_client=PaperBroker(),
    )
    manager.register_account(
        TradingAccount(account_id="PAPER_DISABLED", account_name="Paper Disabled", broker_id="paper", enabled=False),
        broker_client=PaperBroker(),
    )
    return manager


def test_assign_and_resolve():
    assignment = StrategyAssignment(_manager_with_accounts())
    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN")

    assert assignment.has_assignment("DoubleStraddelAlgo") is True
    assert assignment.get_account_id("DoubleStraddelAlgo") == "PAPER_MAIN"


def test_assign_to_unknown_account_raises():
    assignment = StrategyAssignment(_manager_with_accounts())
    with pytest.raises(KeyError):
        assignment.assign("DoubleStraddelAlgo", "NO_SUCH_ACCOUNT")


def test_assign_to_disabled_account_raises():
    assignment = StrategyAssignment(_manager_with_accounts())
    with pytest.raises(InvalidAssignmentError):
        assignment.assign("DoubleStraddelAlgo", "PAPER_DISABLED")


def test_get_account_id_for_unassigned_strategy_raises():
    assignment = StrategyAssignment(_manager_with_accounts())
    with pytest.raises(UnknownAssignmentError):
        assignment.get_account_id("NeverAssigned")


def test_remove_clears_assignment():
    assignment = StrategyAssignment(_manager_with_accounts())
    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN")

    assignment.remove("DoubleStraddelAlgo")

    assert assignment.has_assignment("DoubleStraddelAlgo") is False


def test_remove_unknown_strategy_is_a_no_op():
    assignment = StrategyAssignment(_manager_with_accounts())
    assignment.remove("NeverAssigned")  # must not raise


def test_validate_rechecks_enabled_after_the_fact():
    manager = _manager_with_accounts()
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN")

    manager.get_account("PAPER_MAIN").enabled = False

    with pytest.raises(InvalidAssignmentError):
        assignment.validate("DoubleStraddelAlgo")


def test_validate_passes_for_a_healthy_assignment():
    assignment = StrategyAssignment(_manager_with_accounts())
    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN")

    assignment.validate("DoubleStraddelAlgo")  # must not raise
