"""Phase 14.6 -- safety hardening regression tests for all five blockers
plus the centralized kill switch, all exercised end-to-end through
StrategyExecutionEngine.execute() against a real AngelOneBroker instance
wired to a fake SmartAPI double (no real network, no real credentials --
the same pattern established throughout this repo since Phase 5B/8/9).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trading.common.alerts import AlertManager
from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.angelone import AngelOneBroker
from trading.common.config import BrokerCredentials, TradingConfig
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.idempotency_store import InMemoryIdempotencyStore, SqliteIdempotencyStore
from trading.common.kill_switch import CentralKillSwitch
from trading.common.live_canary import CanaryLimits, LiveCanaryGuard
from trading.common.observability import AuditTrail, MetricsRegistry
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import ExecutionMode, TradingAccount

STRATEGY_ID = "DoubleStraddelAlgo"
SYMBOL = "NIFTY15SEP2623400CE"


class FakeSmartApi:
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


def _stack(
    *,
    account_id: str = "ANGEL_ACCT",
    execution_mode: ExecutionMode = ExecutionMode.LIVE,
    risk_limits: RiskLimits | None = None,
    canary_guard: LiveCanaryGuard | None = None,
    central_kill_switch: CentralKillSwitch | None = None,
    idempotency_store=None,
    metrics: MetricsRegistry | None = None,
    audit_trail: AuditTrail | None = None,
    alerts: AlertManager | None = None,
    trading_mode: str = "live",
):
    fake = FakeSmartApi("ak")
    config = TradingConfig(
        trading_mode=trading_mode,
        credentials=BrokerCredentials(angelone_api_key="ak", angelone_client_id="C1", angelone_password="pw", angelone_totp_secret="JBSWY3DPEHPK3PXP"),
    )
    broker = AngelOneBroker(config, smart_api_factory=lambda k: fake, instrument_resolver=lambda s: ("NFO", "99999"), read_only=False)

    broker_manager = BrokerManager()
    account = TradingAccount(account_id=account_id, account_name="Angel", broker_id="angelone", execution_mode=execution_mode)
    broker_manager.register_account(account, broker_client=broker)
    broker.connect()

    strategy_assignment = StrategyAssignment(broker_manager)
    strategy_assignment.assign(STRATEGY_ID, account_id, execution_mode=execution_mode)
    risk_manager = RiskManager(strategy_assignment, risk_limits)

    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(min_api_interval_seconds=0.0),
        risk_manager=risk_manager, strategy_assignment=strategy_assignment, broker_manager=broker_manager,
        metrics=metrics, audit_trail=audit_trail, alerts=alerts,
        canary_guard=canary_guard, central_kill_switch=central_kill_switch, idempotency_store=idempotency_store,
    )
    return engine, fake, risk_manager


def _intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id=STRATEGY_ID, account_id="ANGEL_ACCT", symbol=SYMBOL, exchange="NFO",
        side=OrderSide.SELL, quantity=1, order_type=OrderType.LIMIT, limit_price=125.5,
        idempotency_key="idem-1", reason="TEST",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


_LIVE_READY_LIMITS = RiskLimits(
    max_order_quantity=5, max_order_value=1000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
    max_orders_per_day=10, max_strategy_exposure=10000.0, max_account_exposure=20000.0,
)


# =========================================================================== #
# BLOCKER A -- structural LIVE_CANARY enforcement
# =========================================================================== #
def test_blocker_a_1_live_canary_with_guard_is_potentially_authorized():
    guard = LiveCanaryGuard(CanaryLimits(
        account_id="ANGEL_ACCT", max_order_quantity=5, max_order_value=1000.0,
        max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=10,
    ))
    engine, fake, _rm = _stack(execution_mode=ExecutionMode.LIVE_CANARY, canary_guard=guard)

    result = engine.execute(_intent())

    assert result.success is True
    fake.placeOrder.assert_called_once()


def test_blocker_a_2_live_canary_with_missing_guard_is_rejected():
    engine, fake, _rm = _stack(execution_mode=ExecutionMode.LIVE_CANARY, canary_guard=None)

    result = engine.execute(_intent())

    assert result.success is False
    assert "LiveCanaryGuard" in result.message
    fake.placeOrder.assert_not_called()


def test_blocker_a_3_live_canary_with_disabled_shutdown_guard_is_rejected():
    guard = LiveCanaryGuard(CanaryLimits(
        account_id="ANGEL_ACCT", max_order_quantity=5, max_order_value=1000.0,
        max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=10,
    ))
    guard.emergency_shutdown("preflight self-test")  # "disabled"
    engine, fake, _rm = _stack(execution_mode=ExecutionMode.LIVE_CANARY, canary_guard=guard)

    result = engine.execute(_intent())

    assert result.success is False
    assert "EMERGENCY_SHUTDOWN" in result.message
    fake.placeOrder.assert_not_called()


def test_blocker_a_4_live_canary_with_kill_switch_is_rejected():
    guard = LiveCanaryGuard(CanaryLimits(
        account_id="ANGEL_ACCT", max_order_quantity=5, max_order_value=1000.0,
        max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=10,
    ))
    guard.engage_kill_switch(reason="halt")
    engine, fake, _rm = _stack(execution_mode=ExecutionMode.LIVE_CANARY, canary_guard=guard)

    result = engine.execute(_intent())

    assert result.success is False
    assert "KILL_SWITCH" in result.message
    fake.placeOrder.assert_not_called()


def test_blocker_a_5_live_canary_with_invalid_limits_cannot_even_construct():
    """CanaryLimits validates at construction (Phase 14) -- an "invalid
    limits" guard can never come into existence in the first place, which
    is the strongest possible form of "rejected"."""
    with pytest.raises(ValueError):
        CanaryLimits(account_id="ANGEL_ACCT", max_order_quantity=0, max_order_value=1000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=10)


def test_blocker_a_mismatched_account_guard_is_rejected():
    """A guard dedicated to a DIFFERENT account than the one being
    executed against is also rejected -- LiveCanaryGuard's own
    DEDICATED_ACCOUNT check, exercised through the full engine."""
    guard = LiveCanaryGuard(CanaryLimits(
        account_id="SOME_OTHER_ACCOUNT", max_order_quantity=5, max_order_value=1000.0,
        max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=10,
    ))
    engine, fake, _rm = _stack(execution_mode=ExecutionMode.LIVE_CANARY, canary_guard=guard)

    result = engine.execute(_intent())

    assert result.success is False
    assert "DEDICATED_ACCOUNT" in result.message
    fake.placeOrder.assert_not_called()


def test_invalid_execution_mode_fails_closed():
    """Phase 14.7: a NORMALLY-constructed TradingAccount can never hold an
    invalid execution_mode (ExecutionMode(...) coercion in __post_init__
    raises ValueError immediately -- verified separately). But
    TradingAccount is a plain, mutable dataclass, so `.execution_mode` CAN
    be set to an arbitrary value post-construction (e.g. by a future bug,
    or a future 5th enum member added without updating execute()'s own
    branching -- exactly the class of gap that caused Blocker A
    originally). This proves execute()'s defensive
    "unrecognized execution_mode" branch is a real, reachable fail-safe,
    not dead code."""
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS)
    engine._broker_manager.get_account("ANGEL_ACCT").execution_mode = "SOME_FUTURE_MODE"

    result = engine.execute(_intent())

    assert result.success is False
    assert "unrecognized execution_mode" in result.message
    fake.placeOrder.assert_not_called()


# =========================================================================== #
# BLOCKER B -- LIVE broker response validation (unconditional now)
# =========================================================================== #
def test_blocker_b_plain_live_rejects_a_malformed_broker_response():
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS)
    fake.placeOrder = MagicMock(return_value="")  # AngelOneBroker maps this to REJECTED -- use a status-forcing double instead

    # Force a genuinely malformed response: monkeypatch place_order at the
    # adapter layer to return something validate_broker_response() must reject.
    from trading.common.broker import OrderResult

    real_place_order = engine._broker.place_order
    engine._broker.place_order = lambda *a, **k: OrderResult(order_id="", symbol=SYMBOL, side=OrderSide.SELL, quantity=1, status="OPEN")

    result = engine.execute(_intent())

    assert result.success is False
    assert "validation" in result.message
    engine._broker.place_order = real_place_order


def test_blocker_b_plain_live_accepts_a_well_formed_broker_response():
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS)

    result = engine.execute(_intent())

    assert result.success is True
    fake.placeOrder.assert_called_once()


def test_plain_live_handles_a_broker_resolution_failure_safely():
    """Phase 14.7 section 4: 'broker failures handled safely' -- if
    BrokerManager.get_broker() itself raises (e.g. an expired/invalid
    session), execute() must return a clean rejection, never propagate
    the raw exception or place an order."""
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS)

    def _broken_get_broker(account_id):
        raise RuntimeError("session expired")

    engine._broker_manager.get_broker = _broken_get_broker

    result = engine.execute(_intent())

    assert result.success is False
    assert "could not resolve broker" in result.message
    fake.placeOrder.assert_not_called()


# =========================================================================== #
# BLOCKER C -- observability failures never affect the execution result
# =========================================================================== #
def test_blocker_c_1_broker_succeeds_and_audit_succeeds():
    audit_trail = AuditTrail()
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS, audit_trail=audit_trail)

    result = engine.execute(_intent())

    assert result.success is True
    assert result.order_id == "REAL-ORDER-1"
    assert engine.observability_health.healthy is True


def test_blocker_c_2_broker_succeeds_and_audit_fails():
    class _BoomAuditTrail:
        def append(self, *a, **k):
            raise RuntimeError("audit trail is broken")

    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS, audit_trail=_BoomAuditTrail())

    result = engine.execute(_intent())

    assert result.success is True
    assert result.order_id == "REAL-ORDER-1"
    fake.placeOrder.assert_called_once()  # the broker call still happened exactly once
    assert engine.observability_health.healthy is False
    assert any(f.component == "audit_trail" for f in engine.observability_health.failures())


def test_blocker_c_3_broker_succeeds_and_metrics_fails():
    class _BoomMetrics:
        def __getattr__(self, name):
            def _boom(*a, **k):
                raise RuntimeError("metrics registry is broken")
            return _boom

    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS, metrics=_BoomMetrics())

    result = engine.execute(_intent())

    assert result.success is True
    assert result.order_id == "REAL-ORDER-1"
    assert engine.observability_health.healthy is False
    assert any(f.component == "metrics" for f in engine.observability_health.failures())


def test_blocker_c_4_broker_succeeds_and_alert_fails():
    """Alerts are only raised today on a REJECTION (there is no alert call
    on a pure success path -- deliberately, there is nothing to alert
    about). This test proves the more general, and more important,
    guarantee: once the alerts layer is known-broken (surfaced by an
    earlier rejection), a LATER, well-formed order still reaches the
    broker and succeeds -- an unhealthy alerts layer never blocks a
    legitimate subsequent order."""
    class _BoomAlerts:
        def __getattr__(self, name):
            def _boom(*a, **k):
                raise RuntimeError("alert manager is broken")
            return _boom

    limits = RiskLimits(**{**_LIVE_READY_LIMITS.__dict__, "max_order_quantity": 1})
    engine, fake, _rm = _stack(risk_limits=limits, alerts=_BoomAlerts())

    rejected = engine.execute(_intent(idempotency_key="over-limit", quantity=99))
    assert rejected.success is False
    assert engine.observability_health.healthy is False
    assert any(f.component == "alerts" for f in engine.observability_health.failures())

    result = engine.execute(_intent(idempotency_key="fine", quantity=1))

    assert result.success is True
    assert result.order_id == "REAL-ORDER-1"
    fake.placeOrder.assert_called_once()


def test_blocker_c_5_multiple_observability_failures_still_preserve_the_result():
    class _BoomEverything:
        def __getattr__(self, name):
            def _boom(*a, **k):
                raise RuntimeError(f"{name} is broken")
            return _boom

        def append(self, *a, **k):
            raise RuntimeError("audit trail is broken")

    engine, fake, _rm = _stack(
        risk_limits=_LIVE_READY_LIMITS, metrics=_BoomEverything(), audit_trail=_BoomEverything(), alerts=_BoomEverything(),
    )

    result = engine.execute(_intent())

    assert result.success is True
    assert result.order_id == "REAL-ORDER-1"
    assert result.correlation_id == _intent().correlation_id or result.strategy_id == STRATEGY_ID
    assert engine.observability_health.healthy is False
    assert len(engine.observability_health.failures()) >= 2


def test_blocker_c_idempotency_store_write_failure_does_not_affect_the_result():
    """Phase 14.7: a persistence WRITE failure (Blocker D's own store) is
    treated with the same Blocker C guarantee as metrics/audit/alerts --
    the broker call already happened, so a failure to record it must
    never make the caller believe the order itself failed. This IS a real
    degradation of Blocker D's own guarantee for this one order (recorded
    via ObservabilityHealth, as documented in _persist_idempotency's own
    comment), but never a crash or a wrong result."""
    class _BoomStore:
        def get(self, key):
            return None

        def put(self, record):
            raise RuntimeError("disk full")

    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS, idempotency_store=_BoomStore())

    result = engine.execute(_intent())

    assert result.success is True
    assert result.order_id == "REAL-ORDER-1"
    fake.placeOrder.assert_called_once()
    assert engine.observability_health.healthy is False
    assert any(f.component == "idempotency_store" for f in engine.observability_health.failures())


# =========================================================================== #
# BLOCKER D -- persistent idempotency, including a restart simulation
# =========================================================================== #
def test_blocker_d_first_request_executes_and_persists(tmp_path):
    store = SqliteIdempotencyStore(tmp_path / "idem.db")
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS, idempotency_store=store)

    result = engine.execute(_intent(idempotency_key="same-key"))

    assert result.success is True
    fake.placeOrder.assert_called_once()
    record = store.get("same-key")
    assert record is not None
    assert record.broker_order_id == "REAL-ORDER-1"


def test_blocker_d_same_key_replays_without_calling_the_broker_again(tmp_path):
    store = SqliteIdempotencyStore(tmp_path / "idem.db")
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS, idempotency_store=store)

    r1 = engine.execute(_intent(idempotency_key="same-key"))
    r2 = engine.execute(_intent(idempotency_key="same-key"))

    assert r1.order_id == r2.order_id == "REAL-ORDER-1"
    fake.placeOrder.assert_called_once()  # NOT called a second time


def test_blocker_d_restart_simulation_same_key_still_does_not_duplicate(tmp_path):
    """THE restart simulation: a brand-new engine (fresh RiskManager, fresh
    in-memory duplicate-key sets -- simulating a process restart) backed
    by a NEW SqliteIdempotencyStore instance pointed at the SAME file
    must still refuse to re-submit the same idempotency key."""
    db_path = tmp_path / "idem.db"
    store1 = SqliteIdempotencyStore(db_path)
    engine1, fake1, _rm1 = _stack(risk_limits=_LIVE_READY_LIMITS, idempotency_store=store1)
    r1 = engine1.execute(_intent(idempotency_key="restart-key"))
    assert r1.success is True
    fake1.placeOrder.assert_called_once()

    # Simulate a full process restart: brand-new engine, brand-new
    # RiskManager (empty in-memory duplicate set), brand-new store
    # instance -- ONLY the file on disk carries over.
    store2 = SqliteIdempotencyStore(db_path)
    engine2, fake2, _rm2 = _stack(risk_limits=_LIVE_READY_LIMITS, idempotency_store=store2)
    r2 = engine2.execute(_intent(idempotency_key="restart-key"))

    assert r2.order_id == r1.order_id == "REAL-ORDER-1"
    fake2.placeOrder.assert_not_called()  # the "post-restart" broker double never sees a call


def test_blocker_d_key_reuse_for_a_different_intent_raises():
    store = InMemoryIdempotencyStore()
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS, idempotency_store=store)

    engine.execute(_intent(idempotency_key="dup-key", quantity=1))

    from trading.common.idempotency_store import IdempotencyKeyReuseError

    with pytest.raises(IdempotencyKeyReuseError):
        engine.execute(_intent(idempotency_key="dup-key", quantity=2))  # different quantity, same key
    fake.placeOrder.assert_called_once()  # the mismatched retry never reaches the broker either


def test_blocker_d_no_store_configured_behaves_exactly_as_before():
    """Backward compatibility: idempotency_store=None (the default) means
    no persistence layer is attached -- execute() must behave exactly as
    it did before Phase 14.6 (relying solely on RiskManager's/
    LiveCanaryGuard's own in-memory duplicate-key sets)."""
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS, idempotency_store=None)
    r1 = engine.execute(_intent(idempotency_key="k1"))
    r2 = engine.execute(_intent(idempotency_key="k1"))
    assert r1.success is True
    # Without a persistent store, RiskManager's own in-memory duplicate
    # check still rejects the second attempt -- but via a DIFFERENT
    # mechanism (risk rejection, not an idempotent replay).
    assert r2.success is False
    assert fake.placeOrder.call_count == 1


# =========================================================================== #
# BLOCKER E -- LIVE requires explicit, positive risk limits
# =========================================================================== #
def test_blocker_e_1_live_with_missing_limits_is_rejected():
    engine, fake, _rm = _stack(risk_limits=None)  # RiskLimits() default -- fully unenforced

    result = engine.execute(_intent())

    assert result.success is False
    assert "LIVE execution blocked" in result.message
    fake.placeOrder.assert_not_called()


def test_blocker_e_2_live_with_zero_limits_is_rejected():
    """Zero must never be silently read as 'unlimited'."""
    engine, fake, _rm = _stack(risk_limits=RiskLimits(
        max_order_quantity=0, max_order_value=1000.0, max_daily_loss=5000.0, max_strategy_loss=5000.0,
        max_orders_per_day=10, max_strategy_exposure=10000.0, max_account_exposure=20000.0,
    ))

    result = engine.execute(_intent())

    assert result.success is False
    fake.placeOrder.assert_not_called()


def test_blocker_e_3_live_with_incomplete_limits_is_rejected():
    engine, fake, _rm = _stack(risk_limits=RiskLimits(max_order_quantity=5, max_order_value=1000.0))

    result = engine.execute(_intent())

    assert result.success is False
    fake.placeOrder.assert_not_called()


def test_blocker_e_4_live_with_valid_explicit_limits_proceeds_to_risk_evaluation():
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS)

    result = engine.execute(_intent())

    assert result.success is True
    fake.placeOrder.assert_called_once()


def _shadow_stack(execution_mode: ExecutionMode):
    """PAPER/SHADOW don't need a real (Angel) adapter at all -- ShadowBroker
    (Phase 4) is a pure, non-networked BrokerClient, matching what a
    genuinely non-live account would actually be wired to. Using it here
    (rather than AngelOneBroker with trading_mode="paper", which has its
    own, unrelated, correct TRADING_MODE!=live gate that would fire first
    and isn't what Blocker E is testing) isolates Blocker E's own gate."""
    from trading.common.brokers.shadow_broker import ShadowBroker

    broker = ShadowBroker()
    broker.connect()
    broker_manager = BrokerManager()
    account = TradingAccount(account_id="SHADOW_ACCT", account_name="Shadow", broker_id="shadow", execution_mode=execution_mode)
    broker_manager.register_account(account, broker_client=broker)
    strategy_assignment = StrategyAssignment(broker_manager)
    strategy_assignment.assign(STRATEGY_ID, "SHADOW_ACCT", execution_mode=execution_mode)
    risk_manager = RiskManager(strategy_assignment, None)  # no RiskLimits configured at all
    engine = StrategyExecutionEngine(
        broker=broker, config=ExecutionConfig(min_api_interval_seconds=0.0),
        risk_manager=risk_manager, strategy_assignment=strategy_assignment, broker_manager=broker_manager,
    )
    return engine


def test_blocker_e_5_paper_and_shadow_work_without_production_risk_configuration():
    engine_paper = _shadow_stack(ExecutionMode.PAPER)
    result_paper = engine_paper.execute(_intent(account_id="SHADOW_ACCT", idempotency_key="paper-1"))
    assert result_paper.success is True

    engine_shadow = _shadow_stack(ExecutionMode.SHADOW)
    result_shadow = engine_shadow.execute(_intent(account_id="SHADOW_ACCT", idempotency_key="shadow-1"))
    assert result_shadow.success is True


# =========================================================================== #
# CENTRALIZED KILL SWITCH
# =========================================================================== #
def test_kill_switch_off_normal_authorization():
    ks = CentralKillSwitch()
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS, central_kill_switch=ks)

    result = engine.execute(_intent())

    assert result.success is True
    fake.placeOrder.assert_called_once()


def test_kill_switch_on_authorization_rejected():
    ks = CentralKillSwitch()
    ks.engage(by="user:1", reason="halt")
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS, central_kill_switch=ks)

    result = engine.execute(_intent())

    assert result.success is False
    assert "kill switch" in result.message.lower()
    fake.placeOrder.assert_not_called()


def test_kill_switch_activated_mid_run_blocks_the_next_order():
    ks = CentralKillSwitch()
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS, central_kill_switch=ks)

    r1 = engine.execute(_intent(idempotency_key="k1"))
    ks.engage(by="user:1", reason="halt mid-run")
    r2 = engine.execute(_intent(idempotency_key="k2"))

    assert r1.success is True
    assert r2.success is False
    assert fake.placeOrder.call_count == 1


def test_kill_switch_blocks_live_canary_too_not_just_live():
    ks = CentralKillSwitch()
    ks.engage(reason="halt")
    guard = LiveCanaryGuard(CanaryLimits(
        account_id="ANGEL_ACCT", max_order_quantity=5, max_order_value=1000.0,
        max_daily_loss=5000.0, max_strategy_loss=5000.0, max_orders_per_day=10,
    ))
    engine, fake, _rm = _stack(execution_mode=ExecutionMode.LIVE_CANARY, canary_guard=guard, central_kill_switch=ks)

    result = engine.execute(_intent())

    assert result.success is False
    fake.placeOrder.assert_not_called()


def test_kill_switch_cannot_accidentally_enable_execution():
    """Disengaging the central kill switch does NOT bypass any other
    gate -- e.g. Blocker E's LIVE-readiness requirement still applies
    independently."""
    ks = CentralKillSwitch()
    ks.engage(reason="halt")
    ks.disengage(by="user:1")
    engine, fake, _rm = _stack(risk_limits=None, central_kill_switch=ks)  # no risk limits configured

    result = engine.execute(_intent())

    assert result.success is False  # still blocked, by Blocker E, not the kill switch
    assert "LIVE execution blocked" in result.message
    fake.placeOrder.assert_not_called()


def test_kill_switch_restart_simulation_resets_to_safe_default():
    """Documented limitation: CentralKillSwitch is in-memory only (see its
    own module docstring) -- a fresh instance (simulating a restart)
    always starts disengaged, the SAFE default. This test pins that
    documented behavior rather than silently assuming persistence."""
    ks_before_restart = CentralKillSwitch()
    ks_before_restart.engage(reason="should not survive a restart")

    ks_after_restart = CentralKillSwitch()  # simulates a fresh process
    assert ks_after_restart.engaged is False


# =========================================================================== #
# Phase 14.7 -- end-to-end "exceeded limit" validation for LIVE mode.
# Each of RiskManager's own checks (Phase 6/14.6) is already unit-tested in
# tests/common/test_risk_manager.py; these prove the SAME checks correctly
# block a real execute() call for a LIVE-ready-configured account, with NO
# broker call in any case.
# =========================================================================== #
def test_live_exceeded_quantity_blocks_before_the_broker():
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS)
    result = engine.execute(_intent(quantity=999))  # exceeds max_order_quantity=5
    assert result.success is False
    assert "MAX_ORDER_QUANTITY" in result.message
    fake.placeOrder.assert_not_called()


def test_live_exceeded_order_value_blocks_before_the_broker():
    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS)
    result = engine.execute(_intent(quantity=5, limit_price=1000.0))  # value 5000 > max_order_value=1000
    assert result.success is False
    assert "ORDER_VALUE_LIMIT" in result.message
    fake.placeOrder.assert_not_called()


def test_live_exceeded_daily_loss_blocks_before_the_broker():
    from trading.common.risk_manager import RiskContext

    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS)
    result = engine.execute(_intent(), RiskContext(daily_pnl=-6000.0))  # breaches max_daily_loss=5000
    assert result.success is False
    assert "MAX_DAILY_LOSS" in result.message
    fake.placeOrder.assert_not_called()


def test_live_exceeded_strategy_loss_blocks_before_the_broker():
    from trading.common.risk_manager import RiskContext

    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS)
    result = engine.execute(_intent(), RiskContext(strategy_pnl=-6000.0))  # breaches max_strategy_loss=5000
    assert result.success is False
    assert "MAX_STRATEGY_LOSS" in result.message
    fake.placeOrder.assert_not_called()


def test_live_exceeded_order_count_blocks_before_the_broker():
    limits = RiskLimits(**{**_LIVE_READY_LIMITS.__dict__, "max_orders_per_day": 1})
    engine, fake, _rm = _stack(risk_limits=limits)
    r1 = engine.execute(_intent(idempotency_key="k1"))
    r2 = engine.execute(_intent(idempotency_key="k2"))
    assert r1.success is True
    assert r2.success is False
    assert "MAX_ORDERS_PER_DAY" in r2.message
    fake.placeOrder.assert_called_once()


def test_live_exceeded_strategy_exposure_blocks_before_the_broker():
    from trading.common.risk_manager import RiskContext

    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS)
    # order value (5 * 125.5 = 627.5) pushed over the 10000 strategy-exposure cap
    result = engine.execute(_intent(), RiskContext(strategy_exposure=9999.0))
    assert result.success is False
    assert "MAX_STRATEGY_EXPOSURE" in result.message
    fake.placeOrder.assert_not_called()


def test_live_exceeded_account_exposure_blocks_before_the_broker():
    from trading.common.risk_manager import RiskContext

    engine, fake, _rm = _stack(risk_limits=_LIVE_READY_LIMITS)
    result = engine.execute(_intent(), RiskContext(account_exposure=19999.0))  # pushes over 20000 cap
    assert result.success is False
    assert "MAX_ACCOUNT_EXPOSURE" in result.message
    fake.placeOrder.assert_not_called()
