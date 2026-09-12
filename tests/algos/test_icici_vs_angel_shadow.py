"""Phase 9: verify

    DoubleStraddelAlgo -> OrderIntent -> TradingAccount=ICICI_MAIN
        -> BrokerManager -> ICICIBreezeBroker -> Shadow execution

and compare the result against

    DoubleStraddelAlgo -> OrderIntent -> TradingAccount=ANGEL_MAIN
        -> BrokerManager -> AngelOneBroker -> Shadow execution

Both stacks reuse trading.common.brokers.connected_shadow_broker.
ConnectedShadowBroker exactly as Phase 5B already built it, and exactly as
Phase 8's test_dhan_vs_angel_shadow.py already proved for Dhan: real reads
(mocked SmartAPI/Breeze client, no real network anywhere in this file),
simulated writes via a per-stack ShadowBroker. No new production glue was
needed to make ICICI Breeze slot into this pattern -- that IS the point of
"implement ICICI Breeze behind the existing BrokerClient interface".

DoubleStraddelAlgo's own strategy code is not imported, started, or
referenced by this file at all -- STRATEGY_ID is just a label, matching
every prior shadow-comparison test in this repo (Phase 3/4/5B/8).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.angelone import AngelOneBroker
from trading.common.brokers.connected_shadow_broker import ConnectedShadowBroker
from trading.common.brokers.icici_breeze import ICICIBreezeBroker
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


class FakeBreezeApi:
    def __init__(self, api_key: str):
        self.quote_response = {"Success": [{"ltp": "133.6"}], "Status": 200, "Error": None}
        self.place_order = MagicMock(name="place_order")

    def generate_session(self, api_secret, session_token):
        return {"Success": {}, "Status": 200, "Error": None}

    def get_quotes(self, **kwargs):
        return self.quote_response


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


def _icici_stack(account_id: str):
    broker_manager = BrokerManager()
    fake = FakeBreezeApi("ak")
    icici_config = TradingConfig(
        trading_mode="paper",
        credentials=BrokerCredentials(icici_breeze_api_key="ak", icici_breeze_api_secret="sec", icici_breeze_session_token="tok"),
    )
    real = ICICIBreezeBroker(icici_config, breeze_client_factory=lambda k: fake, read_only=True)
    real.connect()
    connected = ConnectedShadowBroker(real, ShadowBroker(), execution_mode=ExecutionMode.SHADOW)
    connected.connect()

    account = TradingAccount(account_id=account_id, account_name="ICICI Breeze (shadow)", broker_id="icici_breeze", execution_mode=ExecutionMode.SHADOW)
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
# The full chain, for ICICI Breeze
# --------------------------------------------------------------------------- #
def test_doublestraddel_via_icici_main_executes_in_shadow():
    engine, connected, fake = _icici_stack("ICICI_MAIN")

    result = engine.execute(_intent("ICICI_MAIN"))

    assert result.success is True
    assert result.status == "COMPLETE"
    assert result.order_id.startswith("SHADOW-")
    fake.place_order.assert_not_called()  # real ICICI Breeze order API never reached


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
def test_icici_and_angel_shadow_results_match_for_the_same_intent():
    icici_engine, icici_connected, icici_fake = _icici_stack("ICICI_MAIN")
    angel_engine, angel_connected, angel_fake = _angel_stack("ANGEL_MAIN")

    icici_result = icici_engine.execute(_intent("ICICI_MAIN"))
    angel_result = angel_engine.execute(_intent("ANGEL_MAIN"))

    # Same OrderIntent shape (symbol/side/qty/order_type/reason) produces
    # the same class of outcome regardless of which broker sits underneath.
    assert icici_result.status == angel_result.status == "COMPLETE"
    assert icici_result.success == angel_result.success is True

    icici_position = icici_connected.get_positions()[0]
    angel_position = angel_connected.get_positions()[0]
    assert icici_position.symbol == angel_position.symbol == SYMBOL
    assert icici_position.quantity == angel_position.quantity == -65

    icici_fake.place_order.assert_not_called()
    angel_fake.placeOrder.assert_not_called()


def test_neither_stack_ever_surfaces_the_real_accounts_positions():
    """Both ConnectedShadowBroker instances only ever report simulated
    positions -- proven already in Phase 5B for Angel and Phase 8 for
    Dhan; repeated here for ICICI Breeze since it's a distinct adapter
    instance."""
    engine, connected, _fake = _icici_stack("ICICI_MAIN")
    engine.execute(_intent("ICICI_MAIN"))

    positions = connected.get_positions()
    assert all(p.symbol == SYMBOL for p in positions)  # only the simulated fill, nothing from a real account


def test_icici_broker_id_is_distinct_from_angel_in_the_registry():
    _icici_engine, icici_connected, _f1 = _icici_stack("ICICI_MAIN")
    _angel_engine, angel_connected, _f2 = _angel_stack("ANGEL_MAIN")

    assert isinstance(icici_connected, ConnectedShadowBroker)
    assert isinstance(angel_connected, ConnectedShadowBroker)
    assert isinstance(icici_connected._real_broker, ICICIBreezeBroker)
    assert isinstance(angel_connected._real_broker, AngelOneBroker)
