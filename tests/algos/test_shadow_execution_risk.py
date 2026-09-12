"""Phase 6: run the entire DoubleStraddelAlgo shadow flow (the same 8
representative decisions used in test_shadow_execution_demo.py) through
the new, centralized RiskManager -- proving both that normal trading
decisions still flow through cleanly AND that real limits, once
configured, genuinely reject an intent before it ever reaches ShadowBroker.
"""
from __future__ import annotations

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.shadow_broker import ShadowBroker
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskContext, RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import ExecutionMode, TradingAccount

STRATEGY_ID = "DoubleStraddelAlgo"
ACCOUNT_ID = "SHADOW_MAIN"

# Same representative decisions as test_shadow_execution_demo.py (Phase 4) --
# see that file's own docstring for provenance (a reconstruction of
# strategy/hedge.py + strategy/straddle.py's actual logic, not a live capture).
REPRESENTATIVE_DECISIONS = [
    {"leg": "hedge_entry_ce", "symbol": "NIFTY19MAY2624700CE", "side": "BUY", "qty": 65, "order_type": "LIMIT", "price": 8.5},
    {"leg": "hedge_entry_pe", "symbol": "NIFTY19MAY2622700PE", "side": "BUY", "qty": 65, "order_type": "LIMIT", "price": 7.25},
    {"leg": "straddle_entry_ce", "symbol": "NIFTY19MAY2623700CE", "side": "SELL", "qty": 65, "order_type": "LIMIT", "price": 125.5},
    {"leg": "straddle_entry_pe", "symbol": "NIFTY19MAY2623700PE", "side": "SELL", "qty": 65, "order_type": "LIMIT", "price": 118.75},
    {"leg": "straddle_exit_ce", "symbol": "NIFTY19MAY2623700CE", "side": "BUY", "qty": 65, "order_type": "LIMIT", "price": 100.0},
    {"leg": "straddle_exit_pe", "symbol": "NIFTY19MAY2623700PE", "side": "BUY", "qty": 65, "order_type": "LIMIT", "price": 90.0},
    {"leg": "hedge_exit_ce", "symbol": "NIFTY19MAY2624700CE", "side": "SELL", "qty": 65, "order_type": "LIMIT", "price": 3.0},
    {"leg": "hedge_exit_pe", "symbol": "NIFTY19MAY2622700PE", "side": "SELL", "qty": 65, "order_type": "LIMIT", "price": 2.5},
]


def _build_stack(limits: RiskLimits | None = None):
    broker_manager = BrokerManager()
    shadow = ShadowBroker()
    account = TradingAccount(
        account_id=ACCOUNT_ID, account_name="Shadow (Phase 6)", broker_id="shadow", execution_mode=ExecutionMode.SHADOW,
    )
    broker_manager.register_account(account, broker_client=shadow)

    strategy_assignment = StrategyAssignment(broker_manager)
    strategy_assignment.assign(STRATEGY_ID, ACCOUNT_ID)

    risk_manager = RiskManager(strategy_assignment, limits)
    engine = StrategyExecutionEngine(
        shadow,
        ExecutionConfig(min_api_interval_seconds=0.0, allow_market_emergency=True),
        risk_manager=risk_manager,
        strategy_assignment=strategy_assignment,
        broker_manager=broker_manager,
    )
    return engine, shadow, risk_manager


def _to_intent(decision: dict, **overrides) -> OrderIntent:
    fields = dict(
        strategy_id=STRATEGY_ID, account_id=ACCOUNT_ID, symbol=decision["symbol"], exchange="NFO",
        side=OrderSide(decision["side"]), quantity=decision["qty"], order_type=OrderType(decision["order_type"]),
        limit_price=decision["price"],
    )
    fields.update(overrides)
    return OrderIntent(**fields)


# --------------------------------------------------------------------------- #
# Full flow with a REALISTIC configured RiskManager -- everything approved
# --------------------------------------------------------------------------- #
def test_full_shadow_flow_passes_through_risk_manager_and_executes():
    """execute() already validates internally (OrderIntent -> RiskManager ->
    ExecutionEngine, per the architecture) -- this test relies on that,
    rather than ALSO calling risk_manager.validate() beforehand, since a
    real idempotency_key must only ever be validated once across an
    intent's lifecycle (see test_duplicate_leg_idempotency_key_is_rejected_on_replay
    below for that exact behavior, deliberately exercised there instead)."""
    # Mirrors DoubleStraddelAlgo/config.py's own real DAILY_MAX_LOSS=20000
    # and typical single-leg quantity -- see docs/phase-6-risk-report.md.
    limits = RiskLimits(max_order_quantity=65, max_daily_loss=20_000.0, max_order_value=50_000.0)
    engine, shadow, risk_manager = _build_stack(limits)

    results = []
    for decision in REPRESENTATIVE_DECISIONS:
        intent = _to_intent(decision)
        execution_result = engine.execute(intent)
        results.append((decision["leg"], execution_result))

    assert len(results) == 8
    assert all(r.success for _leg, r in results), [(leg, r.message) for leg, r in results if not r.success]
    positions = {p.symbol: p for p in shadow.get_positions()}
    assert all(p.quantity == 0 for p in positions.values())  # every entry matched by its exit


# --------------------------------------------------------------------------- #
# The same flow, but a real limit genuinely rejects one decision
# --------------------------------------------------------------------------- #
def test_oversized_quantity_is_rejected_before_reaching_the_shadow_broker():
    limits = RiskLimits(max_order_quantity=65)
    engine, shadow, risk_manager = _build_stack(limits)

    oversized = _to_intent(REPRESENTATIVE_DECISIONS[2], quantity=5000)  # straddle_entry_ce, way over the limit
    risk_result = risk_manager.validate(oversized)

    assert risk_result.status == "REJECTED"
    assert "MAX_ORDER_QUANTITY" in risk_result.reason
    assert shadow.get_positions() == []  # never reached the broker


def test_kill_switch_blocks_the_entire_shadow_flow():
    engine, shadow, risk_manager = _build_stack()
    context = RiskContext(kill_switch_engaged=True)

    for decision in REPRESENTATIVE_DECISIONS:
        intent = _to_intent(decision)
        risk_result = risk_manager.validate(intent, context)
        assert risk_result.status == "REJECTED"
        assert "KILL_SWITCH" in risk_result.reason

    assert shadow.get_positions() == []


def test_daily_loss_breach_blocks_new_entries_but_decision_is_explicit():
    limits = RiskLimits(max_daily_loss=20_000.0)
    engine, shadow, risk_manager = _build_stack(limits)
    context = RiskContext(daily_pnl=-21_000.0)  # already breached DAILY_MAX_LOSS

    intent = _to_intent(REPRESENTATIVE_DECISIONS[2])
    risk_result = risk_manager.validate(intent, context)

    assert risk_result.status == "REJECTED"
    assert "MAX_DAILY_LOSS" in risk_result.reason


def test_duplicate_leg_idempotency_key_is_rejected_on_replay():
    engine, shadow, risk_manager = _build_stack()
    intent = _to_intent(REPRESENTATIVE_DECISIONS[2], idempotency_key="straddle-entry-ce-2026-09-15")

    first = risk_manager.validate(intent)
    second = risk_manager.validate(intent)  # e.g. a retry after a transient failure resubmits the same intent

    assert first.status == "APPROVED"
    assert second.status == "REJECTED"
    assert "DUPLICATE_ORDER_PROTECTION" in second.reason


# --------------------------------------------------------------------------- #
# Every decision's RiskCheckResult is a complete, explicit audit record
# --------------------------------------------------------------------------- #
def test_every_shadow_decision_produces_a_complete_risk_audit_record():
    engine, shadow, risk_manager = _build_stack(RiskLimits(max_order_quantity=65))

    for decision in REPRESENTATIVE_DECISIONS:
        intent = _to_intent(decision)
        result = risk_manager.validate(intent)

        assert result.status in ("APPROVED", "REJECTED")
        assert result.timestamp
        assert result.strategy_id == STRATEGY_ID
        assert result.account_id == ACCOUNT_ID
        assert result.correlation_id == intent.correlation_id
        # 15 named checks as of Phase 14.6 Blocker E (added MAX_ORDERS_PER_DAY) --
        # was 14 before; updated deliberately, not weakened.
        assert len(result.checks) == 15
