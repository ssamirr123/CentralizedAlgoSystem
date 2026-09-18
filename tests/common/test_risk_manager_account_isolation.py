"""Phase 15B section 12: RiskLimits must be resolvable per-account, not one
global limit shared by every account in the system."""
from __future__ import annotations

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import TradingAccount


def _setup_two_accounts() -> StrategyAssignment:
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id="ANGEL_SAMIR", account_name="Samir", broker_id="angelone"),
        broker_client=PaperBroker(),
    )
    manager.register_account(
        TradingAccount(account_id="ANGEL_WIFE", account_name="Wife", broker_id="angelone"),
        broker_client=PaperBroker(),
    )
    assignment = StrategyAssignment(manager)
    assignment.assign("StrategyA", "ANGEL_SAMIR")
    assignment.assign("StrategyB", "ANGEL_WIFE")
    return assignment


def _intent(strategy_id, account_id, quantity) -> OrderIntent:
    return OrderIntent(
        strategy_id=strategy_id, account_id=account_id, symbol="NIFTY24950CE", exchange="NFO",
        side=OrderSide.SELL, quantity=quantity, order_type=OrderType.LIMIT, limit_price=100.0,
    )


def test_default_limits_apply_to_every_account_when_no_override_is_set():
    assignment = _setup_two_accounts()
    risk_manager = RiskManager(assignment, limits=RiskLimits(max_order_quantity=10))
    samir_result = risk_manager.validate(_intent("StrategyA", "ANGEL_SAMIR", quantity=20))
    wife_result = risk_manager.validate(_intent("StrategyB", "ANGEL_WIFE", quantity=20))
    assert samir_result.allowed is False
    assert wife_result.allowed is False


def test_account_specific_limits_do_not_leak_to_other_accounts():
    assignment = _setup_two_accounts()
    risk_manager = RiskManager(assignment, limits=RiskLimits(max_order_quantity=10))
    risk_manager.set_account_limits("ANGEL_SAMIR", RiskLimits(max_order_quantity=100))

    # Samir's own, more permissive limit applies to Samir.
    samir_result = risk_manager.validate(_intent("StrategyA", "ANGEL_SAMIR", quantity=50))
    assert samir_result.allowed is True

    # Wife's account is untouched by Samir's override -- still the default (10).
    wife_result = risk_manager.validate(_intent("StrategyB", "ANGEL_WIFE", quantity=50))
    assert wife_result.allowed is False
    assert "MAX_ORDER_QUANTITY" in wife_result.reason


def test_get_limits_is_backward_compatible_with_zero_args():
    assignment = _setup_two_accounts()
    default_limits = RiskLimits(max_order_quantity=10)
    risk_manager = RiskManager(assignment, limits=default_limits)
    assert risk_manager.get_limits() is default_limits


def test_get_limits_with_account_id_returns_the_override():
    assignment = _setup_two_accounts()
    risk_manager = RiskManager(assignment, limits=RiskLimits(max_order_quantity=10))
    override = RiskLimits(max_order_quantity=999)
    risk_manager.set_account_limits("ANGEL_SAMIR", override)
    assert risk_manager.get_limits("ANGEL_SAMIR") is override
    assert risk_manager.get_limits("ANGEL_WIFE") is not override
