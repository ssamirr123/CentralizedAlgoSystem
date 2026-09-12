"""Phase 14: LIVE_CANARY authorization wired into StrategyExecutionEngine
-- proves the full pipeline order:

    Strategy -> OrderIntent -> RiskManager -> LIVE_CANARY authorization
        -> ExecutionEngine -> BrokerAdapter

using the same fake-SmartAPI-double pattern (no real network) every other
adapter test in this repo already uses. Crucially, the broker double's
placeOrder is a MagicMock we control -- these tests prove the canary
authorization gate runs and can reject BEFORE the broker is ever called,
and that a broker's response is validated AFTER it responds.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from trading.common.alerts import AlertManager
from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.angelone import AngelOneBroker
from trading.common.config import BrokerCredentials, TradingConfig
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.live_canary import CanaryLimits, LiveCanaryGuard
from trading.common.observability import AuditTrail, EVENT_LIVE_CANARY_AUTHORIZATION, MetricsRegistry
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskContext, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import ExecutionMode, TradingAccount

STRATEGY_ID = "DoubleStraddelAlgo"
ACCOUNT_ID = "ANGEL_CANARY"
SYMBOL = "NIFTY15SEP2623400CE"


class FakeSmartApi:
    """Real translation logic, fake transport -- placeOrder is a MagicMock
    so tests can both assert on it AND script its return value, exactly
    like the existing Angel adapter test suite."""

    def __init__(self, api_key: str):
        self.session_response = {"status": True, "data": {"refreshToken": "rt"}, "message": "SUCCESS"}
        self.ltp_response = {"status": True, "data": {"ltp": 125.5}}
        self.placeOrder = MagicMock(return_value="REAL-ORDER-1")

    def generateSession(self, client_id, password, totp):
        return self.session_response

    def getfeedToken(self):
        return "ft"

    def ltpData(self, exchange, tradingsymbol, symboltoken):
        return self.ltp_response


def _build_stack(*, limits: CanaryLimits | None = None, live: bool = True):
    metrics = MetricsRegistry()
    audit_trail = AuditTrail()
    alerts = AlertManager(audit_trail=audit_trail)

    fake = FakeSmartApi("ak")
    config = TradingConfig(
        trading_mode="live" if live else "paper",
        credentials=BrokerCredentials(angelone_api_key="ak", angelone_client_id="C1", angelone_password="pw", angelone_totp_secret="JBSWY3DPEHPK3PXP"),
    )
    # read_only=False: this is a LIVE_CANARY stack -- real order submission
    # IS reachable, gated by TRADING_MODE=live (above) and, before that
    # ever runs, by LiveCanaryGuard.authorize() below. Never connects to a
    # real network -- FakeSmartApi is a pure Python double.
    broker = AngelOneBroker(config, smart_api_factory=lambda k: fake, instrument_resolver=lambda s: ("NFO", "99999"), read_only=False)

    broker_manager = BrokerManager(metrics_registry=metrics, audit_trail=audit_trail, alerts=alerts)
    account = TradingAccount(account_id=ACCOUNT_ID, account_name="Angel (canary)", broker_id="angelone", execution_mode=ExecutionMode.LIVE_CANARY)
    broker_manager.register_account(account, broker_client=broker)  # already "connected" (fake)
    broker.connect()

    strategy_assignment = StrategyAssignment(broker_manager)
    strategy_assignment.assign(STRATEGY_ID, ACCOUNT_ID, execution_mode=ExecutionMode.LIVE_CANARY)
    risk_manager = RiskManager(strategy_assignment)

    canary_guard = LiveCanaryGuard(limits or CanaryLimits(
        account_id=ACCOUNT_ID, max_order_quantity=5, max_order_value=10000.0,
        max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=3,
    ), alerts=alerts)

    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(min_api_interval_seconds=0.0),
        risk_manager=risk_manager, strategy_assignment=strategy_assignment, broker_manager=broker_manager,
        metrics=metrics, audit_trail=audit_trail, alerts=alerts, canary_guard=canary_guard,
    )
    return metrics, audit_trail, alerts, canary_guard, engine, fake


def _intent(**overrides) -> OrderIntent:
    defaults = dict(
        strategy_id=STRATEGY_ID, account_id=ACCOUNT_ID, symbol=SYMBOL, exchange="NFO",
        side=OrderSide.SELL, quantity=1, order_type=OrderType.LIMIT, limit_price=125.5,
        idempotency_key="canary-idem-1", reason="CANARY_TEST",
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)


# --------------------------------------------------------------------------- #
# Canary authorization runs BEFORE the broker is ever reached
# --------------------------------------------------------------------------- #
def test_canary_rejection_never_reaches_the_broker():
    metrics, audit_trail, alerts, guard, engine, fake = _build_stack(
        limits=CanaryLimits(account_id=ACCOUNT_ID, max_order_quantity=1, max_order_value=10000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=3),
    )
    result = engine.execute(_intent(quantity=99))  # exceeds max_order_quantity=1

    assert result.success is False
    assert "MAX_ORDER_QUANTITY" in result.message
    fake.placeOrder.assert_not_called()

    canary_events = [r for r in audit_trail.records() if r.event_type == EVENT_LIVE_CANARY_AUTHORIZATION]
    assert len(canary_events) == 1
    assert canary_events[0].detail["status"] == "REJECTED"


def test_kill_switch_engaged_on_the_canary_guard_blocks_before_the_broker():
    metrics, audit_trail, alerts, guard, engine, fake = _build_stack()
    guard.engage_kill_switch(reason="manual halt")

    result = engine.execute(_intent())

    assert result.success is False
    fake.placeOrder.assert_not_called()


def test_dedicated_account_check_rejects_a_mismatched_account():
    metrics, audit_trail, alerts, guard, engine, fake = _build_stack()
    # Force a mismatch: guard is bound to ACCOUNT_ID, but the intent (still
    # routed to the same assignment) is hand-crafted to target a different
    # account_id than the guard's own dedicated account.
    bad_limits = CanaryLimits(account_id="SOME_OTHER_ACCOUNT", max_order_quantity=5, max_order_value=10000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=3)
    metrics2, audit_trail2, alerts2, _guard2, engine2, fake2 = _build_stack(limits=bad_limits)

    result = engine2.execute(_intent())

    assert result.success is False
    assert "DEDICATED_ACCOUNT" in result.message
    fake2.placeOrder.assert_not_called()


# --------------------------------------------------------------------------- #
# Canary-authorized order DOES reach the broker (this is what LIVE_CANARY is for)
# --------------------------------------------------------------------------- #
def test_canary_authorized_tiny_order_reaches_the_broker_and_is_recorded():
    metrics, audit_trail, alerts, guard, engine, fake = _build_stack()

    result = engine.execute(_intent())

    assert result.success is True
    fake.placeOrder.assert_called_once()  # the ONLY component that ever calls placeOrder is AngelOneBroker itself
    canary_events = [r for r in audit_trail.records() if r.event_type == EVENT_LIVE_CANARY_AUTHORIZATION]
    assert len(canary_events) == 1
    assert canary_events[0].detail["status"] == "AUTHORIZED"


def test_daily_order_cap_is_enforced_across_multiple_executions():
    metrics, audit_trail, alerts, guard, engine, fake = _build_stack(
        limits=CanaryLimits(account_id=ACCOUNT_ID, max_order_quantity=5, max_order_value=10000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=1),
    )
    r1 = engine.execute(_intent(idempotency_key="k1"))
    r2 = engine.execute(_intent(idempotency_key="k2"))

    assert r1.success is True
    assert r2.success is False
    assert "MAX_ORDERS_PER_DAY" in r2.message
    fake.placeOrder.assert_called_once()


def test_kill_switch_context_and_canary_kill_switch_are_independent_gates():
    """RiskManager's own KILL_SWITCH check (via RiskContext) and
    LiveCanaryGuard's kill switch are two SEPARATE mechanisms -- either one
    alone is sufficient to block an order."""
    metrics, audit_trail, alerts, guard, engine, fake = _build_stack()

    result = engine.execute(_intent(), RiskContext(kill_switch_engaged=True))

    assert result.success is False
    fake.placeOrder.assert_not_called()
    # The canary guard's own authorize() was never reached -- RiskManager
    # rejected first, exactly matching the documented pipeline order.
    canary_events = [r for r in audit_trail.records() if r.event_type == EVENT_LIVE_CANARY_AUTHORIZATION]
    assert len(canary_events) == 0


def test_a_falsy_broker_order_id_is_a_safe_rejection_not_a_crash():
    """AngelOneBroker itself already maps a falsy placeOrder() return into
    a REJECTED OrderResult (Phase 2) -- LiveCanaryGuard.validate_broker_
    response() explicitly allows REJECTED-with-no-order_id (see
    tests/common/test_live_canary.py's dedicated check-10 unit tests for
    the direct proof of that rule); this test only confirms the two layers
    compose without either raising."""
    metrics, audit_trail, alerts, guard, engine, fake = _build_stack()
    fake.placeOrder = MagicMock(return_value="")

    result = engine.execute(_intent(idempotency_key="k-empty"))

    assert result.success is False
    assert result.status == "REJECTED"
