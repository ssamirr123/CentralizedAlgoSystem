"""Phase 13: end-to-end observability wiring across the full stack --

    Strategy -> OrderIntent -> Risk Decision -> Execution -> Broker Order
        -> Fill -> Position -> P&L

-- proving every stage of one order's lifecycle is traceable by a single
correlation_id in a tamper-evident AuditTrail, with metrics and alerts
updated at each stage. Reuses the exact same ConnectedShadowBroker +
AngelOneBroker(read_only=True) stack pattern Phase 5B/8/9 already
established (FakeSmartApi double, no real network, no real credentials)
-- Phase 13 adds observability collaborators on top, nothing else changes.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trading.common.alerts import AlertManager
from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.angelone import AngelOneBroker
from trading.common.brokers.connected_shadow_broker import ConnectedShadowBroker
from trading.common.brokers.shadow_broker import ShadowBroker
from trading.common.config import BrokerCredentials, TradingConfig
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.observability import (
    EVENT_BROKER_CONNECTED,
    EVENT_BROKER_ORDER_PLACED,
    EVENT_EXECUTION_RESULT,
    EVENT_FILL,
    EVENT_ORDER_INTENT_CREATED,
    EVENT_RISK_DECISION,
    AuditTrail,
    MetricsRegistry,
)
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskContext, RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy import StrategyStatus
from trading.common.strategies.double_straddle import DoubleStraddleStrategy
from trading.common.trading_account import ExecutionMode, TradingAccount

STRATEGY_ID = "DoubleStraddelAlgo"
ACCOUNT_ID = "ANGEL_MAIN"
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


def _build_stack(*, limits: RiskLimits | None = None):
    metrics = MetricsRegistry()
    audit_trail = AuditTrail()
    alerts = AlertManager(audit_trail=audit_trail)

    broker_manager = BrokerManager(metrics_registry=metrics, audit_trail=audit_trail, alerts=alerts)
    fake = FakeSmartApi("ak")
    angel_config = TradingConfig(
        trading_mode="paper",
        credentials=BrokerCredentials(angelone_api_key="ak", angelone_client_id="C1", angelone_password="pw", angelone_totp_secret="JBSWY3DPEHPK3PXP"),
    )
    real = AngelOneBroker(angel_config, smart_api_factory=lambda k: fake, instrument_resolver=lambda s: ("NFO", "99999"), read_only=True)

    account = TradingAccount(account_id=ACCOUNT_ID, account_name="Angel (shadow)", broker_id="angelone", execution_mode=ExecutionMode.SHADOW)
    broker_manager.register_account(account, broker_factory=lambda: ConnectedShadowBroker(real, ShadowBroker(), execution_mode=ExecutionMode.SHADOW))

    strategy_assignment = StrategyAssignment(broker_manager)
    strategy_assignment.assign(STRATEGY_ID, ACCOUNT_ID)
    risk_manager = RiskManager(strategy_assignment, limits or RiskLimits())

    strategy = DoubleStraddleStrategy(metrics_registry=metrics, audit_trail=audit_trail, alerts=alerts)
    engine = StrategyExecutionEngine(
        broker=ShadowBroker(),  # never actually used by execute(); see execution_bridge.py's own precedent
        config=ExecutionConfig(min_api_interval_seconds=0.0),
        risk_manager=risk_manager, strategy_assignment=strategy_assignment, broker_manager=broker_manager,
        metrics=metrics, audit_trail=audit_trail, alerts=alerts,
    )
    return metrics, audit_trail, alerts, engine, strategy, fake


def _intent() -> OrderIntent:
    return OrderIntent(
        strategy_id=STRATEGY_ID, account_id=ACCOUNT_ID, symbol=SYMBOL, exchange="NFO",
        side=OrderSide.SELL, quantity=65, order_type=OrderType.LIMIT, limit_price=125.5, reason="MORNING_ENTRY",
    )


# --------------------------------------------------------------------------- #
# Full trace: every stage present, in order, under one correlation_id
# --------------------------------------------------------------------------- #
def test_full_order_lifecycle_is_traceable_by_correlation_id():
    metrics, audit_trail, alerts, engine, strategy, fake = _build_stack()
    intent = _intent()

    result = engine.execute(intent)

    assert result.success is True
    assert result.status == "COMPLETE"

    trace = audit_trail.trace(intent.correlation_id)
    event_types = [r.event_type for r in trace]
    assert event_types == [
        EVENT_ORDER_INTENT_CREATED, EVENT_RISK_DECISION, EVENT_EXECUTION_RESULT,
        EVENT_BROKER_ORDER_PLACED, EVENT_FILL,
    ]
    # Every record in the trace carries the strategy_id too -- not just the
    # correlation_id -- so a caller can filter either way.
    assert all(r.strategy_id == STRATEGY_ID for r in trace)

    # The broker-connect event (a DIFFERENT stage, not part of this
    # specific order's own trace) is also present in the full trail,
    # proving BrokerManager's own wiring fired independently.
    all_events = [r.event_type for r in audit_trail.records()]
    assert EVENT_BROKER_CONNECTED in all_events

    fake.placeOrder.assert_not_called()  # still never reaches the real Angel order API


def test_audit_trail_hash_chain_is_intact_after_a_real_run():
    metrics, audit_trail, alerts, engine, strategy, fake = _build_stack()
    engine.execute(_intent())
    assert audit_trail.verify() is True


def test_metrics_updated_at_every_stage():
    metrics, audit_trail, alerts, engine, strategy, fake = _build_stack()
    engine.execute(_intent())

    snap = metrics.snapshot()
    assert snap.counters[f"order_intents:{STRATEGY_ID}"] == 1
    assert snap.counters[f"orders_approved:{STRATEGY_ID}"] == 1
    assert snap.counters[f"simulated_fills:{STRATEGY_ID}"] == 1
    assert f"execution_latency:{STRATEGY_ID}" in snap.latencies
    assert snap.gauges["broker_heartbeat:angelone"] > 0
    assert snap.statuses["account_status:ANGEL_MAIN"] == "CONNECTED"


# --------------------------------------------------------------------------- #
# Rejection path: risk-rejected intent still produces a partial, honest trace
# --------------------------------------------------------------------------- #
def test_risk_rejected_intent_stops_the_trace_after_risk_decision():
    limits = RiskLimits(max_order_quantity=10)  # intent quantity is 65 -> rejected
    metrics, audit_trail, alerts, engine, strategy, fake = _build_stack(limits=limits)
    intent = _intent()

    result = engine.execute(intent)

    assert result.success is False
    trace = audit_trail.trace(intent.correlation_id)
    # ALERT_RISK_BREACH also carries this correlation_id -- the alert
    # itself is part of this order's own trace, not a separate stream.
    assert [r.event_type for r in trace] == [EVENT_ORDER_INTENT_CREATED, EVENT_RISK_DECISION, "ALERT_RISK_BREACH"]

    snap = metrics.snapshot()
    assert snap.counters[f"orders_rejected:{STRATEGY_ID}"] == 1
    assert snap.counters.get(f"orders_approved:{STRATEGY_ID}", 0) == 0

    risk_alerts = [a for a in alerts.alerts() if a.alert_type == "RISK_BREACH"]
    assert len(risk_alerts) == 1
    assert risk_alerts[0].detail["check_name"] == "MAX_ORDER_QUANTITY"


def test_kill_switch_engaged_context_produces_a_kill_switch_alert():
    metrics, audit_trail, alerts, engine, strategy, fake = _build_stack()
    intent = _intent()

    result = engine.execute(intent, RiskContext(kill_switch_engaged=True))

    assert result.success is False
    kill_alerts = [a for a in alerts.alerts() if a.alert_type == "KILL_SWITCH"]
    assert len(kill_alerts) == 1
    assert kill_alerts[0].severity == "CRITICAL"
    audit_kill_events = [r for r in audit_trail.records() if r.event_type == "ALERT_KILL_SWITCH"]
    assert len(audit_kill_events) == 1


# --------------------------------------------------------------------------- #
# Strategy lifecycle: start/stop emit heartbeat + audit + alert
# --------------------------------------------------------------------------- #
def test_strategy_start_records_heartbeat_and_audit_event():
    metrics, audit_trail, alerts, engine, strategy, fake = _build_stack()
    strategy.enable()
    strategy.start()

    assert strategy.get_status() == StrategyStatus.SHADOW
    snap = metrics.snapshot()
    assert snap.gauges[f"strategy_heartbeat:{STRATEGY_ID}"] > 0
    started_events = [r for r in audit_trail.records() if r.event_type == "STRATEGY_STARTED"]
    assert len(started_events) == 1


def test_strategy_stop_raises_a_strategy_stopped_alert():
    metrics, audit_trail, alerts, engine, strategy, fake = _build_stack()
    strategy.enable()
    strategy.start()
    strategy.stop()

    stopped_alerts = [a for a in alerts.alerts() if a.alert_type == "STRATEGY_STOPPED"]
    assert len(stopped_alerts) == 1
    assert stopped_alerts[0].detail["strategy_id"] == STRATEGY_ID
    stopped_events = [r for r in audit_trail.records() if r.event_type == "STRATEGY_STOPPED"]
    assert len(stopped_events) == 1


# --------------------------------------------------------------------------- #
# Broker connect/disconnect wiring
# --------------------------------------------------------------------------- #
def test_broker_connect_failure_raises_broker_disconnected_alert():
    metrics = MetricsRegistry()
    audit_trail = AuditTrail()
    alerts = AlertManager(audit_trail=audit_trail)
    broker_manager = BrokerManager(metrics_registry=metrics, audit_trail=audit_trail, alerts=alerts)

    def _broken_factory():
        raise RuntimeError("simulated connect failure")

    broker_manager.register_account(
        TradingAccount(account_id="BROKEN", account_name="Broken", broker_id="angelone"),
        broker_factory=_broken_factory,
    )

    with pytest.raises(RuntimeError):
        broker_manager.get_broker("BROKEN")

    disconnect_alerts = [a for a in alerts.alerts() if a.alert_type == "BROKER_DISCONNECTED"]
    assert len(disconnect_alerts) == 1
    assert metrics.snapshot().statuses["account_status:BROKEN"] == "ERROR"
