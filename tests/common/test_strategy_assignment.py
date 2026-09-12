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
from trading.common.trading_account import ExecutionMode, TradingAccount


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


# --------------------------------------------------------------------------- #
# Assignment record: execution_mode, risk_profile, enabled -- Phase 7
# --------------------------------------------------------------------------- #
def test_assignment_adopts_the_accounts_own_execution_mode_by_default():
    manager = _manager_with_accounts()  # PAPER_MAIN defaults to ExecutionMode.PAPER
    assignment = StrategyAssignment(manager)

    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN")

    record = assignment.get_assignment("DoubleStraddelAlgo")
    assert record.execution_mode == ExecutionMode.PAPER
    assert record.risk_profile == "default"
    assert record.enabled is True


def test_assign_rejects_an_execution_mode_that_does_not_match_the_account():
    manager = _manager_with_accounts()  # PAPER_MAIN is ExecutionMode.PAPER
    assignment = StrategyAssignment(manager)

    with pytest.raises(InvalidAssignmentError, match="execution_mode"):
        assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN", execution_mode=ExecutionMode.SHADOW)


def test_assign_accepts_an_execution_mode_that_matches_the_account():
    manager = _manager_with_accounts()
    assignment = StrategyAssignment(manager)

    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN", execution_mode=ExecutionMode.PAPER)

    assert assignment.get_assignment("DoubleStraddelAlgo").execution_mode == ExecutionMode.PAPER


def test_assign_accepts_a_custom_risk_profile():
    manager = _manager_with_accounts()
    assignment = StrategyAssignment(manager)

    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN", risk_profile="conservative")

    assert assignment.get_assignment("DoubleStraddelAlgo").risk_profile == "conservative"


def test_assign_can_create_a_disabled_assignment():
    manager = _manager_with_accounts()
    assignment = StrategyAssignment(manager)

    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN", enabled=False)

    assert assignment.get_assignment("DoubleStraddelAlgo").enabled is False


def test_validate_rejects_a_disabled_assignment_even_if_the_account_is_enabled():
    manager = _manager_with_accounts()
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN", enabled=False)

    with pytest.raises(InvalidAssignmentError, match="disabled"):
        assignment.validate("DoubleStraddelAlgo")


# --------------------------------------------------------------------------- #
# Broker-level unavailability blocks assignment -- Phase 7
# --------------------------------------------------------------------------- #
def test_assign_rejects_when_the_accounts_broker_is_marked_unavailable():
    from trading.common.broker_manager import BrokerUnavailableError

    manager = _manager_with_accounts()
    manager.set_broker_availability("paper", False, reason="maintenance")
    assignment = StrategyAssignment(manager)

    with pytest.raises(BrokerUnavailableError, match="maintenance"):
        assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN")


def test_validate_rejects_once_the_accounts_broker_becomes_unavailable():
    from trading.common.broker_manager import BrokerUnavailableError

    manager = _manager_with_accounts()
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN")

    manager.set_broker_availability("paper", False, reason="outage")

    with pytest.raises(BrokerUnavailableError):
        assignment.validate("DoubleStraddelAlgo")
