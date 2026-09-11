"""trading/common/risk_manager.py -- the pre-trade interception point."""
from __future__ import annotations

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskContext, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import TradingAccount


def _assignment_with(account_id="PAPER_MAIN", enabled=True) -> StrategyAssignment:
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id=account_id, account_name="Paper", broker_id="paper", enabled=enabled),
        broker_client=PaperBroker(),
    )
    assignment = StrategyAssignment(manager)
    if enabled:
        assignment.assign("DoubleStraddelAlgo", account_id)
    return assignment


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="DoubleStraddelAlgo",
        account_id="PAPER_MAIN",
        symbol="NIFTY24950CE",
        exchange="NFO",
        side=OrderSide.SELL,
        quantity=50,
        order_type=OrderType.LIMIT,
        limit_price=125.5,
    )
    fields.update(overrides)
    return OrderIntent(**fields)


def test_valid_intent_is_allowed():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent())
    assert result.allowed is True
    assert result.reason == ""


def test_non_positive_quantity_is_denied():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(quantity=0))
    assert result.allowed is False
    assert "quantity" in result.reason


def test_unknown_strategy_assignment_is_denied():
    assignment = _assignment_with()  # only "DoubleStraddelAlgo" is assigned
    risk_manager = RiskManager(assignment)
    result = risk_manager.validate(_intent(strategy_id="SomeOtherAlgo"))
    assert result.allowed is False
    assert "assignment" in result.reason.lower()


def test_disabled_account_is_denied():
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id="PAPER_MAIN", account_name="Paper", broker_id="paper", enabled=True),
        broker_client=PaperBroker(),
    )
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", "PAPER_MAIN")
    risk_manager = RiskManager(assignment)

    manager.get_account("PAPER_MAIN").enabled = False

    result = risk_manager.validate(_intent())
    assert result.allowed is False
    assert "disabled" in result.reason.lower()


def test_limit_order_requires_limit_price():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(limit_price=None))
    assert result.allowed is False
    assert "limit_price" in result.reason


def test_market_order_does_not_require_limit_price():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(order_type=OrderType.MARKET, limit_price=None))
    assert result.allowed is True


def test_disabled_strategy_context_is_denied():
    risk_manager = RiskManager(_assignment_with())
    result = risk_manager.validate(_intent(), RiskContext(strategy_enabled=False))
    assert result.allowed is False
    assert "disabled" in result.reason.lower()
