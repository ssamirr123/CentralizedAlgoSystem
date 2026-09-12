"""Phase 4: run DoubleStraddelAlgo's actual trading decisions through the
full broker-agnostic architecture in shadow mode --

    OrderIntent -> RiskManager -> ExecutionEngine -> ShadowBroker -> Simulated OrderResult

This is a deliberate, separate demo/comparison harness -- it does NOT touch
trading/algos/DoubleStraddelAlgo/broker/execution_bridge.py's existing
Phase 3 stack (which stays on the hard-coded-unconnectable AngelOneBroker,
unchanged, per its own test suite) and it does NOT start the live algo
process. The decisions simulated here are representative reconstructions
of what strategy/hedge.py and strategy/straddle.py actually do (per the
architecture-baseline audit), not a live capture -- DoubleStraddelAlgo's
own process is never started.

Every OrderIntent here is additionally cross-checked against
execution_bridge.compare_with_legacy() (the Phase 3 comparison tool,
reused rather than duplicated) to prove the new representation would
generate the same trading instruction as the existing Angel-specific one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from trading.common.broker import OrderSide, OrderType
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.shadow_broker import ShadowBroker
from trading.common.execution import ExecutionConfig, StrategyExecutionEngine
from trading.common.order_intent import OrderIntent
from trading.common.risk_manager import RiskManager
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.trading_account import ExecutionMode, TradingAccount

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALGO_ROOT = _REPO_ROOT / "trading" / "algos" / "DoubleStraddelAlgo" / "broker"
if str(_ALGO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_ALGO_ROOT.parent))
if str(_REPO_ROOT / "trading" / "algos" / "DoubleStraddelAlgo") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "trading" / "algos" / "DoubleStraddelAlgo"))

from broker.execution_bridge import compare_with_legacy  # noqa: E402  (Phase 3 tool, reused not duplicated)

STRATEGY_ID = "DoubleStraddelAlgo"
ACCOUNT_ID = "SHADOW_MAIN"

# Representative reconstruction of DoubleStraddelAlgo's actual decisions
# (see strategy/hedge.py::enter_hedge/exit_hedge and
# strategy/straddle.py::enter_straddle/_close_leg) -- NOT a live capture.
# tok values are illustrative Angel symboltokens, matching the shape
# token_file.token_nifty() returns.
REPRESENTATIVE_DECISIONS = [
    {"leg": "hedge_entry_ce", "symbol": "NIFTY19MAY2624700CE", "token": "51001", "side": "BUY", "qty": 65, "order_type": "LIMIT", "price": 8.5, "reason": "HEDGE_ENTRY"},
    {"leg": "hedge_entry_pe", "symbol": "NIFTY19MAY2622700PE", "token": "51002", "side": "BUY", "qty": 65, "order_type": "LIMIT", "price": 7.25, "reason": "HEDGE_ENTRY"},
    {"leg": "straddle_entry_ce", "symbol": "NIFTY19MAY2623700CE", "token": "48521", "side": "SELL", "qty": 65, "order_type": "LIMIT", "price": 125.5, "reason": "MORNING_ENTRY"},
    {"leg": "straddle_entry_pe", "symbol": "NIFTY19MAY2623700PE", "token": "48522", "side": "SELL", "qty": 65, "order_type": "LIMIT", "price": 118.75, "reason": "MORNING_ENTRY"},
    {"leg": "straddle_exit_ce", "symbol": "NIFTY19MAY2623700CE", "token": "48521", "side": "BUY", "qty": 65, "order_type": "LIMIT", "price": 100.0, "reason": "SL"},
    {"leg": "straddle_exit_pe", "symbol": "NIFTY19MAY2623700PE", "token": "48522", "side": "BUY", "qty": 65, "order_type": "LIMIT", "price": 90.0, "reason": "Target"},
    {"leg": "hedge_exit_ce", "symbol": "NIFTY19MAY2624700CE", "token": "51001", "side": "SELL", "qty": 65, "order_type": "LIMIT", "price": 3.0, "reason": "FINAL_EXIT"},
    {"leg": "hedge_exit_pe", "symbol": "NIFTY19MAY2622700PE", "token": "51002", "side": "SELL", "qty": 65, "order_type": "LIMIT", "price": 2.5, "reason": "FINAL_EXIT"},
]


def _build_demo_stack():
    broker_manager = BrokerManager()
    shadow = ShadowBroker()
    account = TradingAccount(
        account_id=ACCOUNT_ID, account_name="Shadow (Phase 4 demo)", broker_id="shadow",
        execution_mode=ExecutionMode.SHADOW,
    )
    broker_manager.register_account(account, broker_client=shadow)

    strategy_assignment = StrategyAssignment(broker_manager)
    strategy_assignment.assign(STRATEGY_ID, ACCOUNT_ID)

    risk_manager = RiskManager(strategy_assignment)
    engine = StrategyExecutionEngine(
        shadow,
        ExecutionConfig(min_api_interval_seconds=0.0, allow_market_emergency=True),
        risk_manager=risk_manager,
        strategy_assignment=strategy_assignment,
        broker_manager=broker_manager,
    )
    return engine, shadow


def _to_intent(decision: dict) -> OrderIntent:
    return OrderIntent(
        strategy_id=STRATEGY_ID,
        account_id=ACCOUNT_ID,
        symbol=decision["symbol"],
        exchange="NFO",
        side=OrderSide(decision["side"]),
        quantity=decision["qty"],
        order_type=OrderType(decision["order_type"]),
        limit_price=decision["price"],
        reason=decision["reason"],
        metadata={"angelone_symboltoken": decision["token"], "leg": decision["leg"]},
    )


@pytest.fixture(scope="module")
def demo_run():
    """Run every representative decision through the full architecture
    ONCE per test module, and hand the (engine, shadow, results) to every
    test -- avoids re-running the whole demo per assertion."""
    engine, shadow = _build_demo_stack()
    results = []
    for decision in REPRESENTATIVE_DECISIONS:
        intent = _to_intent(decision)
        result = engine.execute(intent)
        comparison = compare_with_legacy(
            decision["symbol"], decision["token"], decision["qty"], decision["side"],
            decision["order_type"], decision["price"], intent,
        )
        results.append({"decision": decision, "intent": intent, "result": result, "comparison": comparison})
    return {"engine": engine, "shadow": shadow, "results": results}


# --------------------------------------------------------------------------- #
# Counts (quoted directly in docs/phase-4-shadow-report.md)
# --------------------------------------------------------------------------- #
def test_number_of_decisions_and_intents_match_one_to_one(demo_run):
    assert len(REPRESENTATIVE_DECISIONS) == 8
    assert len(demo_run["results"]) == 8
    # One OrderIntent generated per strategy decision -- no duplication, no drops.
    intent_ids = {r["intent"].client_order_id for r in demo_run["results"]}
    assert len(intent_ids) == 8


# --------------------------------------------------------------------------- #
# Every decision executes successfully through the real pipeline
# --------------------------------------------------------------------------- #
def test_every_representative_decision_fills_successfully(demo_run):
    for entry in demo_run["results"]:
        result = entry["result"]
        assert result.success is True, f"{entry['decision']['leg']} failed: {result.message}"
        assert result.status == "COMPLETE"


def test_every_result_carries_correlation_strategy_and_timestamp(demo_run):
    for entry in demo_run["results"]:
        result = entry["result"]
        assert result.correlation_id
        assert result.strategy_id == STRATEGY_ID
        assert result.created_at
        assert result.account_id == ACCOUNT_ID


# --------------------------------------------------------------------------- #
# Comparison: existing execution decision vs new OrderIntent
# --------------------------------------------------------------------------- #
def test_every_decision_matches_its_legacy_reconstruction(demo_run):
    for entry in demo_run["results"]:
        comparison = entry["comparison"]
        assert comparison["match"] is True, f"{entry['decision']['leg']}: {comparison['differences']}"


def test_hedge_legs_are_buy_and_straddle_entries_are_sell(demo_run):
    by_leg = {r["decision"]["leg"]: r for r in demo_run["results"]}
    assert by_leg["hedge_entry_ce"]["intent"].side == OrderSide.BUY
    assert by_leg["hedge_entry_pe"]["intent"].side == OrderSide.BUY
    assert by_leg["straddle_entry_ce"]["intent"].side == OrderSide.SELL
    assert by_leg["straddle_entry_pe"]["intent"].side == OrderSide.SELL


# --------------------------------------------------------------------------- #
# Positions after a full simulated day
# --------------------------------------------------------------------------- #
def test_positions_are_flat_after_entries_and_exits(demo_run):
    """Every entered leg was also exited in REPRESENTATIVE_DECISIONS -- the
    simulated end-of-day position book should be flat, same invariant the
    real EOD square-off (strategy/engine.py's 15:25 exit) targets."""
    shadow = demo_run["shadow"]
    positions = {p.symbol: p for p in shadow.get_positions()}
    for symbol, position in positions.items():
        assert position.quantity == 0, f"{symbol} not flat: qty={position.quantity}"


def test_finding_execute_ignores_intent_limit_price(demo_run):
    """IMPORTANT PHASE 4 FINDING (see docs/phase-4-shadow-report.md):
    StrategyExecutionEngine.place_limit() -- the code path execute() uses
    -- computes its OWN price from broker.get_quote() plus configured
    slippage; it does NOT use OrderIntent.limit_price at all. The
    straddle_entry_ce decision above requested price=125.5, but the
    simulated fill happened at ShadowBroker's synthetic quote price
    instead. This test documents the gap rather than asserting a P&L
    outcome that depends on a price the pipeline doesn't actually honor
    today -- see the report's "Differences" section and recommendation."""
    by_leg = {r["decision"]["leg"]: r for r in demo_run["results"]}
    entry = by_leg["straddle_entry_ce"]
    requested_price = entry["decision"]["price"]

    shadow = demo_run["shadow"]
    quote = shadow.get_quote("NIFTY19MAY2623700CE")

    # The requested price was NOT what the simulated fill used -- it came
    # from the broker's live quote instead. If this assertion ever starts
    # failing, it means execute() has started honoring limit_price, which
    # would be a welcome fix worth updating this test (and the report) for.
    assert requested_price != quote.last_price
    assert entry["result"].success is True  # it still filled -- just at a different price than requested


def test_positions_show_a_result_even_though_pnl_does_not_reflect_strategy_intent(demo_run):
    """Positions end flat (see test_positions_are_flat_after_entries_and_exits)
    and DO carry a P&L number -- but per the finding above, that number
    reflects the broker's synthetic quote + retry slippage, not the
    strategy's actual intended entry/exit prices (125.5 -> 100.0, a real
    25.5-point profit). Asserting a specific sign/magnitude here would be
    asserting the bug, not the behavior -- so this test only proves a
    number was produced, deliberately not what it says."""
    shadow = demo_run["shadow"]
    positions = {p.symbol: p for p in shadow.get_positions()}
    assert isinstance(positions["NIFTY19MAY2623700CE"].pnl, float)
    assert isinstance(positions["NIFTY19MAY2623700PE"].pnl, float)


def test_no_real_broker_api_was_ever_reachable(demo_run):
    """Structural guard, not a behavioral one: ShadowBroker literally has
    no SmartAPI/network code (see test_shadow_broker.py), so this demo
    could not have reached Angel One regardless of what it simulated."""
    from trading.common.brokers.shadow_broker import ShadowBroker

    assert isinstance(demo_run["shadow"], ShadowBroker)
