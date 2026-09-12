"""
Phase 11 -- process-wide wiring of the broker-agnostic execution framework
(Phases 1-10, trading/common/*) for the control-center API to read/control.

Nothing here calls a broker, connects to anything, or places an order.
Every object built by build_execution_state() is exactly the same kind of
in-memory object the Phase 1-10 test suites already construct -- this
module only centralizes ONE instance of each per FastAPI app so
trading/api/execution_routes.py's endpoints have something to read.

State lives on `app.state.execution` (an ExecutionState), built fresh
inside create_app() -- NOT as a module-level global. This matters for test
isolation: tests/conftest.py's `app` fixture calls create_app() once per
test, so each test gets its own fresh BrokerManager/StrategyRegistry/etc.
with no risk of one test's kill-switch/assignment/strategy-lifecycle state
leaking into the next (the same class of hazard Phase 5A hit and fixed for
module-level env-var mutation).

Example accounts (ANGEL_MAIN/DHAN_MAIN/ICICI_MAIN) mirror the Phase 7
reference registry exactly: all execution_mode=SHADOW, Dhan/ICICI Breeze
marked broker-unavailable (Phase 8/9 built those adapters but neither has
been validated against a real account yet, so this phase does not change
their production-routing posture). Each account's BrokerClient is built
via a LAZY factory (trading.common.broker.create_broker(), the exact same
factory production would use) -- but no endpoint in execution_routes.py
ever calls BrokerManager.get_broker() on it, so that factory is never
actually invoked and no broker SDK/network/credential is ever touched by
this API. Accounts are listed by static TradingAccount metadata only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from trading.common.alerts import AlertManager
from trading.common.broker import create_broker
from trading.common.broker_manager import BrokerManager
from trading.common.config import TradingConfig
from trading.common.kill_switch import CentralKillSwitch
from trading.common.observability import AuditTrail, MetricsRegistry
from trading.common.risk_manager import RiskManager
from trading.common.strategies.combined_vwap_nifty import CombinedVwapNiftyStrategy
from trading.common.strategies.double_straddle import DoubleStraddleStrategy
from trading.common.strategies.vwap_algo_nifty_hedge import VwapAlgoNiftyHedgeStrategy
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_registry import StrategyRegistry
from trading.common.trading_account import ExecutionMode, TradingAccount

_EXAMPLE_ACCOUNTS = (
    ("ANGEL_MAIN", "Angel One (main)", "angelone"),
    ("DHAN_MAIN", "Dhan (main)", "dhan"),
    ("ICICI_MAIN", "ICICI Breeze (main)", "icici_breeze"),
)


@dataclass
class ExecutionState:
    broker_manager: BrokerManager
    strategy_assignment: StrategyAssignment
    risk_manager: RiskManager
    strategy_registry: StrategyRegistry
    # Phase 14.6: was a locally-defined KillSwitch dataclass here, entirely
    # disconnected from trading.common.live_canary.LiveCanaryGuard's own
    # kill switch (a real gap Phase 14.5's review flagged explicitly).
    # CentralKillSwitch's public interface (engaged/engaged_by/reason/
    # engaged_at/disengaged_at, engage()/disengage()) is an exact match for
    # the old KillSwitch class, so trading/api/execution_routes.py's
    # kill-switch endpoint needed ZERO changes -- only this construction
    # site changed. A StrategyExecutionEngine built against this
    # ExecutionState and passed this SAME instance as its own
    # central_kill_switch is what makes "engage via the API" and "blocked
    # in execute()" the same switch, closing that gap.
    kill_switch: CentralKillSwitch = field(default_factory=CentralKillSwitch)
    # Phase 13 observability -- one MetricsRegistry/AuditTrail/AlertManager
    # per app instance, same test-isolation rationale as everything else
    # on this dataclass (see module docstring).
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    audit_trail: AuditTrail = field(default_factory=AuditTrail)
    alerts: AlertManager = field(default_factory=AlertManager)


def build_execution_state() -> ExecutionState:
    metrics = MetricsRegistry()
    audit_trail = AuditTrail()
    alerts = AlertManager(audit_trail=audit_trail)

    broker_manager = BrokerManager(metrics_registry=metrics, audit_trail=audit_trail, alerts=alerts)
    for account_id, account_name, broker_id in _EXAMPLE_ACCOUNTS:
        account = TradingAccount(
            account_id=account_id, account_name=account_name, broker_id=broker_id,
            execution_mode=ExecutionMode.SHADOW,
        )
        broker_manager.register_account(
            account,
            broker_factory=lambda broker_id=broker_id: create_broker(
                TradingConfig(broker_name=broker_id, trading_mode="paper")
            ),
        )
    broker_manager.set_broker_availability(
        "dhan", False, reason="Dhan adapter (Phase 8) not yet validated against a real account"
    )
    broker_manager.set_broker_availability(
        "icici_breeze", False, reason="ICICI Breeze adapter (Phase 9) not yet validated against a real account"
    )

    strategy_assignment = StrategyAssignment(broker_manager)
    risk_manager = RiskManager(strategy_assignment)

    strategy_registry = StrategyRegistry()
    strategy_registry.register(DoubleStraddleStrategy(metrics_registry=metrics, audit_trail=audit_trail, alerts=alerts))
    strategy_registry.register(CombinedVwapNiftyStrategy(metrics_registry=metrics, audit_trail=audit_trail, alerts=alerts))
    strategy_registry.register(VwapAlgoNiftyHedgeStrategy(metrics_registry=metrics, audit_trail=audit_trail, alerts=alerts))

    return ExecutionState(
        broker_manager=broker_manager,
        strategy_assignment=strategy_assignment,
        risk_manager=risk_manager,
        strategy_registry=strategy_registry,
        metrics=metrics,
        audit_trail=audit_trail,
        alerts=alerts,
    )
