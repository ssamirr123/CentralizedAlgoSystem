"""End-to-end: OrderIntent -> RiskManager -> StrategyExecutionEngine.execute()
-> StrategyAssignment -> TradingAccount -> BrokerManager -> PaperBroker.

Exercises the Phase 1 architectural wiring using only PaperBroker and
in-memory collaborators -- no real network/broker calls anywhere. This is
the proof that a "strategy" (represented here by nothing more than an
OrderIntent + a call to execute()) never has to import, construct, or even
know the name of the concrete broker underneath its assigned account.
"""
from __future__ import annotations

import pytest

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import TradingAccount

ACCOUNT_ID = "PAPER_MAIN"
STRATEGY_ID = "DoubleStraddelAlgo"


def _build_stack(account_enabled: bool = True, assign: bool = True):
    broker_manager = BrokerManager()
    paper = PaperBroker()
    paper.connect()
    account = TradingAccount(
        account_id=ACCOUNT_ID, account_name="Paper Trading", broker_id="paper", enabled=account_enabled
    )
    broker_manager.register_account(account, broker_client=paper)

    strategy_assignment = StrategyAssignment(broker_manager)
    if assign and account_enabled:
        strategy_assignment.assign(STRATEGY_ID, ACCOUNT_ID)

    risk_manager = RiskManager(strategy_assignment)

    engine = StrategyExecutionEngine(
        paper,
        ExecutionConfig(min_api_interval_seconds=0.0),
        risk_manager=risk_manager,
        strategy_assignment=strategy_assignment,
        broker_manager=broker_manager,
    )
    return engine, broker_manager, strategy_assignment, paper


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id=STRATEGY_ID,
        account_id=ACCOUNT_ID,
        symbol="NIFTY24950CE",
        exchange="NFO",
        side=OrderSide.SELL,
        quantity=50,
        order_type=OrderType.LIMIT,
        limit_price=125.50,
        reason="ENTRY",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


# Test 1: create a TradingAccount using PaperBroker.
def test_trading_account_created_with_paper_broker():
    _, broker_manager, _, paper = _build_stack()
    account = broker_manager.get_account(ACCOUNT_ID)
    assert account.broker_id == "paper"
    assert broker_manager.get_broker(ACCOUNT_ID) is paper


# Test 2: assign a strategy to the PaperBroker account.
def test_strategy_assigned_to_paper_account():
    _, _, strategy_assignment, _ = _build_stack()
    assert strategy_assignment.get_account_id(STRATEGY_ID) == ACCOUNT_ID


# Test 3: create an OrderIntent.
def test_order_intent_construction():
    intent = _intent()
    assert intent.side == OrderSide.SELL
    assert intent.order_type == OrderType.LIMIT
    assert intent.client_order_id


# Test 4 + 5: pass the intent through the full chain; verify it reaches PaperBroker.
def test_intent_reaches_paper_broker_end_to_end():
    engine, _, _, paper = _build_stack()
    result = engine.execute(_intent())

    assert result.success is True
    assert result.status == "FILLED"
    assert result.order_id.startswith("PAPER-")
    assert result.account_id == ACCOUNT_ID
    assert result.broker_id == "paper"
    assert paper.get_positions()  # PaperBroker actually recorded the fill


# Test 6: the strategy side never needs to know PaperBroker exists -- the
# OrderIntent/ExecutionResult shapes carry no broker-specific field, and
# execute() is called the same way regardless of which broker backs the
# assigned account (see test_order_intent.py's structural field-name guard
# for the complementary check on OrderIntent itself).
def test_execution_result_carries_no_broker_specific_shape():
    engine, _, _, _ = _build_stack()
    result = engine.execute(_intent())

    forbidden = {"smartapi", "breeze", "dhan", "shoonya", "variety", "producttype", "symboltoken"}
    field_names = {f.lower() for f in result.__dataclass_fields__.keys()}
    assert field_names.isdisjoint(forbidden)


# Test 7: invalid intents are rejected before reaching the broker.
def test_invalid_intent_rejected_before_reaching_broker():
    engine, _, _, paper = _build_stack()
    result = engine.execute(_intent(quantity=0))

    assert result.success is False
    assert result.status == "REJECTED"
    assert paper.get_positions() == []


# Test 8: an unknown strategy assignment is rejected.
def test_unknown_strategy_assignment_rejected():
    engine, _, _, paper = _build_stack()
    result = engine.execute(_intent(strategy_id="NoSuchStrategy"))

    assert result.success is False
    assert "assignment" in result.message.lower()
    assert paper.get_positions() == []


# Test 9: a disabled trading account is rejected.
def test_disabled_trading_account_rejected():
    engine, broker_manager, _, paper = _build_stack(account_enabled=True)
    broker_manager.get_account(ACCOUNT_ID).enabled = False

    result = engine.execute(_intent())

    assert result.success is False
    assert "disabled" in result.message.lower()
    assert paper.get_positions() == []


def test_execute_without_collaborators_raises_a_clear_error():
    """A plain StrategyExecutionEngine(broker) -- today's only supported
    construction -- must keep working exactly as before; execute() is opt-in."""
    paper = PaperBroker()
    paper.connect()
    engine = StrategyExecutionEngine(paper)

    with pytest.raises(RuntimeError, match="risk_manager"):
        engine.execute(_intent())


# Test 10: existing StrategyExecutionEngine tests continue to pass -- see
# tests/common/test_execution.py, run unmodified alongside this file.
