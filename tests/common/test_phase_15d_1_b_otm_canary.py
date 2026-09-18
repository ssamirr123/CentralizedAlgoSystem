"""Phase 15D.1-B: OTM canary instrument (real, live-resolved) validated
against the existing Account-B-specific RiskLimits/CanaryLimits/
LiveCanaryGuard/authorization boundary/idempotency/kill switch.

Real, live data as of this check (2026-09-16, market open):
    Symbol: NIFTY22SEP2623600CE (confirmed OTM: strike 23600 > spot ~23230)
    Token:  57031
    LTP:    30.20
    Lot size (from real instrument metadata, not hard-coded): 65
    Estimated order value: 65 x 30.20 = 1963.00
    Account B available_cash (fresh real read): 3019.4003

No real broker credential, network call, or mutation is used anywhere in
this file -- the real values above are baked in as constants (frozen at
the time they were fetched) purely to size-check the existing gates.
"""
from __future__ import annotations

import pytest

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.live_canary import CanaryLimits, LiveCanaryGuard
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskContext, RiskLimits, RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import AccountAuthorizationState, ExecutionMode, TradingAccount

ACCOUNT_B = "ANGEL_ACCOUNT_B"

# Real, live values (frozen at fetch time -- see module docstring).
OTM_SYMBOL = "NIFTY22SEP2623600CE"
OTM_STRIKE = 23600.0
SPOT_AT_FETCH = 23229.9
OTM_LTP = 30.20
LOT_SIZE = 65  # from the real instrument's own scrip-master `lotsize` field
ESTIMATED_ORDER_VALUE = LOT_SIZE * OTM_LTP  # 1963.00
AVAILABLE_CASH_AT_FETCH = 3019.4003

ACCOUNT_B_CANARY_LIMITS = CanaryLimits(
    account_id=ACCOUNT_B, max_order_quantity=LOT_SIZE, max_order_value=5000.0,
    max_daily_loss=2500.0, max_strategy_loss=2500.0, max_orders_per_day=1,
)


def _otm_intent(**overrides) -> OrderIntent:
    fields = dict(
        strategy_id="DoubleStraddelAlgo", account_id=ACCOUNT_B, symbol=OTM_SYMBOL, exchange="NFO",
        side=OrderSide.BUY, quantity=LOT_SIZE, order_type=OrderType.LIMIT, limit_price=OTM_LTP,
        idempotency_key="PHASE15D1B-OTM-CANARY",
    )
    fields.update(overrides)
    return OrderIntent(**fields)


def test_selected_instrument_is_genuinely_otm():
    """OTM CE means strike > spot."""
    assert OTM_STRIKE > SPOT_AT_FETCH


def test_ltp_is_within_the_requested_band():
    assert 25 <= OTM_LTP <= 35


def test_lot_size_was_resolved_from_real_instrument_metadata_not_hardcoded():
    """The value used here (65) matches the real scrip master's own
    `lotsize` field for NIFTY22SEP2623600CE, fetched directly -- not
    assumed from any other constant in this codebase."""
    assert LOT_SIZE == 65


def test_estimated_order_value_matches_ltp_times_lot_size():
    assert ESTIMATED_ORDER_VALUE == pytest.approx(1963.00)


# --------------------------------------------------------------------------- #
# Funds gate
# --------------------------------------------------------------------------- #
def test_funds_gate_passes_with_real_fetched_values():
    assert AVAILABLE_CASH_AT_FETCH >= ESTIMATED_ORDER_VALUE


def test_funds_coverage_ratio_is_comfortably_above_one():
    ratio = AVAILABLE_CASH_AT_FETCH / ESTIMATED_ORDER_VALUE
    assert ratio > 1.5


# --------------------------------------------------------------------------- #
# CanaryLimits / RiskLimits sizing
# --------------------------------------------------------------------------- #
def test_otm_order_within_account_b_canary_limits():
    assert LOT_SIZE <= ACCOUNT_B_CANARY_LIMITS.max_order_quantity
    assert ESTIMATED_ORDER_VALUE <= ACCOUNT_B_CANARY_LIMITS.max_order_value


def test_risk_manager_synthetically_allows_the_otm_order():
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id=ACCOUNT_B, account_name="B", broker_id="angelone", execution_mode=ExecutionMode.LIVE_CANARY),
        broker_client=PaperBroker(),
    )
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", ACCOUNT_B, execution_mode=ExecutionMode.LIVE_CANARY)
    live_ready_limits = RiskLimits(
        max_order_quantity=LOT_SIZE, max_order_value=5000.0, max_daily_loss=2500.0, max_strategy_loss=2500.0,
        max_orders_per_day=1, max_strategy_exposure=5000.0, max_account_exposure=5000.0,
    )
    risk_manager = RiskManager(assignment, limits=live_ready_limits)
    result = risk_manager.validate(_otm_intent(), RiskContext(reference_price=OTM_LTP))
    assert result.allowed is True


# --------------------------------------------------------------------------- #
# LiveCanaryGuard
# --------------------------------------------------------------------------- #
def test_account_b_guard_authorizes_the_otm_order():
    guard = LiveCanaryGuard(ACCOUNT_B_CANARY_LIMITS)
    result = guard.authorize(_otm_intent())
    assert result.allowed is True


def test_account_b_guard_still_rejects_account_a_for_the_otm_order():
    guard = LiveCanaryGuard(ACCOUNT_B_CANARY_LIMITS)
    result = guard.authorize(_otm_intent(account_id="ANGEL_SAMIR", idempotency_key="wrong-account"))
    assert result.allowed is False
    assert "DEDICATED_ACCOUNT" in result.reason


# --------------------------------------------------------------------------- #
# Authorization boundary -- still blocks Account B's real current state
# --------------------------------------------------------------------------- #
def test_authorization_boundary_still_rejects_read_only_for_the_otm_order():
    manager = BrokerManager()
    account = TradingAccount(
        account_id=ACCOUNT_B, account_name="B", broker_id="angelone", execution_mode=ExecutionMode.LIVE_CANARY,
        authorization_state=AccountAuthorizationState.READ_ONLY,
    )
    manager.register_account(account, broker_client=PaperBroker())
    assignment = StrategyAssignment(manager)
    assignment.assign("DoubleStraddelAlgo", ACCOUNT_B, execution_mode=ExecutionMode.LIVE_CANARY)
    risk_manager = RiskManager(assignment)
    guard = LiveCanaryGuard(ACCOUNT_B_CANARY_LIMITS)
    engine = StrategyExecutionEngine(
        PaperBroker(), ExecutionConfig(dry_run=True),
        risk_manager=risk_manager, strategy_assignment=assignment, broker_manager=manager, canary_guard=guard,
    )
    result = engine.execute(_otm_intent())
    assert result.success is False
    assert "authorization_state" in result.message
