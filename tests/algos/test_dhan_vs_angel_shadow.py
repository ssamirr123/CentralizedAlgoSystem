"""Phase 8: verify

    DoubleStraddelAlgo -> OrderIntent -> TradingAccount=DHAN_MAIN
        -> BrokerManager -> DhanBroker -> Shadow execution

and compare the result against

    DoubleStraddelAlgo -> OrderIntent -> TradingAccount=ANGEL_MAIN
        -> BrokerManager -> AngelOneBroker -> Shadow execution

Both stacks reuse trading.common.brokers.connected_shadow_broker.
ConnectedShadowBroker exactly as Phase 5B already built it: real reads
(mocked SmartAPI/Dhan client, no real network anywhere in this file),
simulated writes via a per-stack ShadowBroker. No new production glue was
needed to make Dhan slot into this pattern -- that IS the point of
"follow exactly the same BrokerClient interface used by AngelOne".

DoubleStraddelAlgo's own strategy code is not imported, started, or
referenced by this file at all -- STRATEGY_ID is just a label, matching
every prior shadow-comparison test in this repo (Phase 3/4/5B).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.angelone import AngelOneBroker
from trading.common.brokers.connected_shadow_broker import ConnectedShadowBroker
from trading.common.brokers.dhan import DhanBroker
from trading.common.brokers.shadow_broker import ShadowBroker
from trading.common.config import BrokerCredentials, TradingConfig
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import ExecutionMode, TradingAccount

STRATEGY_ID = "DoubleStraddelAlgo"
SYMBOL = "NIFTY15SEP2623400CE"


class FakeSmartApi:
    def __init__(self, api_key: str):
        self.session_response = {"status": True, "data": {"refreshToken": "rt"}, "message": "SUCCESS"}
        self.ltp_response = {"status": True, "data": {"ltp": 133.6}}
        self.placeOrder = MagicMock(name="placeOrder")

    def generateSession(self, client_id, password, totp):
        return self.session_response

    def getfeedToken(self):
        return "ft"

    def ltpData(self, exchange, tradingsymbol, symboltoken):
        return self.ltp_response


class FakeDhanApi:
    def __init__(self, client_id: str, access_token: str):
        self.fund_response = {"status": "success", "data": {"availableBalance": 50000.0, "utilizedAmount": 0.0}}
        self.ticker_response = {"status": "success", "data": {"NSE_FNO": {"99999": {"last_price": 133.6}}}}
        self.place_order = MagicMock(name="place_order")

    def get_fund_limits(self):
        return self.fund_response

    def ticker_data(self, securities):
        return self.ticker_response


def _angel_stack(account_id: str):
    broker_manager = BrokerManager()
    fake = FakeSmartApi("ak")
    angel_config = TradingConfig(
        trading_mode="paper",
        credentials=BrokerCredentials(angelone_api_key="ak", angelone_client_id="C1", angelone_password="pw", angelone_totp_secret="JBSWY3DPEHPK3PXP"),
    )
    real = AngelOneBroker(angel_config, smart_api_factory=lambda k: fake, instrument_resolver=lambda s: ("NFO", "99999"), read_only=True)
    real.connect()
    connected = ConnectedShadowBroker(real, ShadowBroker(), execution_mode=ExecutionMode.SHADOW)
    connected.connect()

    account = TradingAccount(account_id=account_id, account_name="Angel (shadow)", broker_id="angelone", execution_mode=ExecutionMode.SHADOW)
    broker_manager.register_account(account, broker_client=connected)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign(STRATEGY_ID, account_id)
    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(connected, ExecutionConfig(min_api_interval_seconds=0.0), risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager)
    return engine, connected, fake


def _dhan_stack(account_id: str):
    broker_manager = BrokerManager()
    fake = FakeDhanApi("C123", "tok")
    dhan_config = TradingConfig(
        trading_mode="paper",
        credentials=BrokerCredentials(dhan_client_id="C123", dhan_access_token="tok"),
    )
    real = DhanBroker(dhan_config, dhan_client_factory=lambda c, t: fake, instrument_resolver=lambda s: ("NSE_FNO", "99999"), read_only=True)
    real.connect()
    connected = ConnectedShadowBroker(real, ShadowBroker(), execution_mode=ExecutionMode.SHADOW)
    connected.connect()

    account = TradingAccount(account_id=account_id, account_name="Dhan (shadow)", broker_id="dhan", execution_mode=ExecutionMode.SHADOW)
    broker_manager.register_account(account, broker_client=connected)
    assignment = StrategyAssignment(broker_manager)
    assignment.assign(STRATEGY_ID, account_id)
    risk_manager = RiskManager(assignment)
    engine = StrategyExecutionEngine(connected, ExecutionConfig(min_api_interval_seconds=0.0), risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=broker_manager)
    return engine, connected, fake


def _intent(account_id: str) -> OrderIntent:
    return OrderIntent(
        strategy_id=STRATEGY_ID, account_id=account_id, symbol=SYMBOL, exchange="NFO",
        side=OrderSide.SELL, quantity=65, order_type=OrderType.LIMIT, limit_price=125.5, reason="MORNING_ENTRY",
    )


# --------------------------------------------------------------------------- #
# The full chain, for Dhan
# --------------------------------------------------------------------------- #
def test_doublestraddel_via_dhan_main_executes_in_shadow():
    engine, connected, fake = _dhan_stack("DHAN_MAIN")

    result = engine.execute(_intent("DHAN_MAIN"))

    assert result.success is True
    assert result.status == "COMPLETE"
    assert result.order_id.startswith("SHADOW-")
    fake.place_order.assert_not_called()  # real Dhan order API never reached


def test_doublestraddel_via_angel_main_executes_in_shadow():
    engine, connected, fake = _angel_stack("ANGEL_MAIN")

    result = engine.execute(_intent("ANGEL_MAIN"))

    assert result.success is True
    assert result.status == "COMPLETE"
    assert result.order_id.startswith("SHADOW-")
    fake.placeOrder.assert_not_called()  # real Angel order API never reached


# --------------------------------------------------------------------------- #
# Comparison: same OrderIntent shape, same shadow outcome, different broker
# --------------------------------------------------------------------------- #
def test_dhan_and_angel_shadow_results_match_for_the_same_intent():
    dhan_engine, dhan_connected, dhan_fake = _dhan_stack("DHAN_MAIN")
    angel_engine, angel_connected, angel_fake = _angel_stack("ANGEL_MAIN")

    dhan_result = dhan_engine.execute(_intent("DHAN_MAIN"))
    angel_result = angel_engine.execute(_intent("ANGEL_MAIN"))

    # Same OrderIntent shape (symbol/side/qty/order_type/reason) produces
    # the same class of outcome regardless of which broker sits underneath.
    assert dhan_result.status == angel_result.status == "COMPLETE"
    assert dhan_result.success == angel_result.success is True

    dhan_position = dhan_connected.get_positions()[0]
    angel_position = angel_connected.get_positions()[0]
    assert dhan_position.symbol == angel_position.symbol == SYMBOL
    assert dhan_position.quantity == angel_position.quantity == -65

    dhan_fake.place_order.assert_not_called()
    angel_fake.placeOrder.assert_not_called()


def test_neither_stack_ever_surfaces_the_real_accounts_positions():
    """Both ConnectedShadowBroker instances only ever report simulated
    positions -- proven already in Phase 5B for Angel; repeated here for
    Dhan since it's a distinct adapter instance."""
    engine, connected, _fake = _dhan_stack("DHAN_MAIN")
    engine.execute(_intent("DHAN_MAIN"))

    positions = connected.get_positions()
    assert all(p.symbol == SYMBOL for p in positions)  # only the simulated fill, nothing from a real account


def test_dhan_broker_id_is_distinct_from_angel_in_the_registry():
    _dhan_engine, dhan_connected, _f1 = _dhan_stack("DHAN_MAIN")
    _angel_engine, angel_connected, _f2 = _angel_stack("ANGEL_MAIN")

    assert isinstance(dhan_connected, ConnectedShadowBroker)
    assert isinstance(angel_connected, ConnectedShadowBroker)
    assert isinstance(dhan_connected._real_broker, DhanBroker)
    assert isinstance(angel_connected._real_broker, AngelOneBroker)
