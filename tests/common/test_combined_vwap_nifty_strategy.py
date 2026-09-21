"""Phase 16.7: trading/common/strategies/combined_vwap_nifty.py -- the ONE
strategy integrated with real (ported) decision logic and normalized
market data this phase. Proves the complete

    Market Data -> Strategy -> OrderIntent -> StrategyExecutionEngine -> PAPER/SHADOW

flow using CombinedVwapNifty's real, ported arm/fire entry rule and
3-level combined-loss exit ladder -- never a fabricated signal.
"""
from __future__ import annotations

import threading

from trading.common.broker import OrderSide
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.kill_switch import CentralKillSwitch
from trading.common.market_data_gateway import FixedMarketDataSource
from trading.common.risk_manager import RiskLimits, RiskManager
from trading.common.strategies.combined_vwap_nifty import RISK_LOSS_LEVELS, CombinedVwapNiftyStrategy
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_registry import StrategyRegistry
from trading.common.strategy_runtime import StrategyRuntime
from trading.common.trading_account import ExecutionMode, TradingAccount
from trading.market_data.schemas import OptionQuote

STRATEGY_ID = "CombinedVwapNifty"
CE = "NIFTY_CE_ATM"
PE = "NIFTY_PE_ATM"


def _opt_quote(symbol: str, ltp: float) -> OptionQuote:
    import datetime as dt

    return OptionQuote(
        symbol=symbol, underlying="NIFTY", expiry=dt.date(2026, 9, 25), strike=24950.0,
        option_type="CE" if "CE" in symbol else "PE", ltp=ltp,
    )


def _strategy(**kw) -> CombinedVwapNiftyStrategy:
    s = CombinedVwapNiftyStrategy(account_id="ACC1", **kw)
    s.enable()
    s.start()
    return s


def _feed(strategy: CombinedVwapNiftyStrategy, ce_ltp: float, pe_ltp: float):
    market_data = {CE: _opt_quote(CE, ce_ltp), PE: _opt_quote(PE, pe_ltp)}
    return strategy.generate_order_intents(market_data)


# --------------------------------------------------------------------------- #
# required_instruments()
# --------------------------------------------------------------------------- #
def test_declares_ce_and_pe_as_required_instruments():
    strategy = CombinedVwapNiftyStrategy(ce_instrument=CE, pe_instrument=PE)
    assert strategy.required_instruments() == (CE, PE)


# --------------------------------------------------------------------------- #
# No-trade scenarios (critical, per this phase's own instruction)
# --------------------------------------------------------------------------- #
def test_no_market_data_produces_zero_intents():
    strategy = _strategy()
    assert strategy.generate_order_intents(None) == []
    assert strategy.generate_order_intents({}) == []


def test_partial_market_data_produces_zero_intents():
    strategy = _strategy()
    assert strategy.generate_order_intents({CE: _opt_quote(CE, 120.0)}) == []


def test_never_armed_condition_produces_zero_intents():
    """cp <= cv on every call -- the arm condition (cp > cv, manager.py:84)
    never fires, so the state machine never reaches ARMED and never
    enters. A constant premium keeps cp exactly equal to its own running
    average (cv), so cp > cv is never true."""
    strategy = _strategy()
    for _ in range(5):
        intents = _feed(strategy, 100.0, 100.0)
        assert intents == []


def test_armed_but_never_fired_produces_zero_intents():
    """cp > cv arms it, but cp never subsequently drops below cv (cv > cp,
    manager.py:88) -- still zero intents, since arming alone is not
    entry."""
    strategy = _strategy()
    intents1 = _feed(strategy, 130.0, 130.0)  # first call: cv==cp (first sample) -> not > cv, no arm yet
    assert intents1 == []
    intents2 = _feed(strategy, 200.0, 200.0)  # cp=400 > running cv avg(260,400)=... arms
    assert intents2 == []
    intents3 = _feed(strategy, 250.0, 250.0)  # cp=500, still likely > cv (rising) -- stays armed, no fire
    assert intents3 == []


# --------------------------------------------------------------------------- #
# Real entry -- the ported arm/fire rule
# --------------------------------------------------------------------------- #
def test_arm_then_fire_generates_a_sell_intent_for_both_legs():
    strategy = _strategy()
    _feed(strategy, 150.0, 150.0)  # cp=300, cv=300 (first sample) -- not armed yet (cp>cv false, equal)
    _feed(strategy, 200.0, 200.0)  # cp=400 > cv(=300) -> ARMED
    intents = _feed(strategy, 90.0, 90.0)  # cp=180, cv=(300+400+180)/3=293.3 -> cv>cp -> FIRE

    assert len(intents) == 2
    symbols = {i.symbol for i in intents}
    assert symbols == {CE, PE}
    for intent in intents:
        assert intent.side == OrderSide.SELL
        assert intent.strategy_id == STRATEGY_ID
        assert intent.account_id == "ACC1"
        assert intent.quantity == 65
        assert intent.idempotency_key != ""


def test_repeated_identical_evaluation_after_entry_does_not_reenter():
    """Same market data fed twice after entry -- the second call must not
    generate a duplicate entry (leg.in_position gates it)."""
    strategy = _strategy()
    _feed(strategy, 150.0, 150.0)
    _feed(strategy, 200.0, 200.0)
    first = _feed(strategy, 90.0, 90.0)
    assert len(first) == 2

    second = _feed(strategy, 90.0, 90.0)
    assert second == []  # already in position on both legs -- no re-entry, no re-fire (state reset to WAIT_ARM)


# --------------------------------------------------------------------------- #
# Exit: the ported 3-level combined-loss ladder
# --------------------------------------------------------------------------- #
def _enter(strategy: CombinedVwapNiftyStrategy, entry_price: float = 100.0) -> None:
    _feed(strategy, entry_price, entry_price)
    _feed(strategy, entry_price + 50, entry_price + 50)
    fired = _feed(strategy, entry_price - 60, entry_price - 60)
    assert len(fired) == 2


def test_below_threshold_loss_produces_no_exit():
    strategy = _strategy(quantity=1)
    _enter(strategy, entry_price=100.0)  # fires at ltp=40 for both legs (see _enter's own price sequence)
    # small adverse move -- combined unrealized loss (qty=1) well under 650
    intents = _feed(strategy, 45.0, 45.0)
    assert intents == []


def test_level_0_breach_exits_only_the_bigger_loser_leg():
    strategy = _strategy(quantity=1)  # qty=1 so loss-in-rupees == price delta, easy to reason about
    _enter(strategy, entry_price=100.0)
    # CE moves against us hard, PE barely -- combined loss crosses 650
    # but CE is clearly the bigger loser.
    ce_ltp, pe_ltp = 100.0 + 500.0, 100.0 + 10.0  # unrealized: CE=500, PE=10 -> combined=510... need >=650
    ce_ltp, pe_ltp = 100.0 + 640.0, 100.0 + 20.0  # combined=660 >= 650
    intents = _feed(strategy, ce_ltp, pe_ltp)
    assert len(intents) == 1
    assert intents[0].symbol == CE
    assert intents[0].side == OrderSide.BUY
    assert strategy._risk_level_index == 1
    assert strategy._legs[CE].in_position is False
    assert strategy._legs[PE].in_position is True  # untouched


def test_level_2_breach_exits_both_legs_and_stops_the_day():
    """The ladder advances exactly one level per breach (manager.py's own
    ratcheting design, matched exactly -- a single evaluation cannot skip
    from level 0 straight to level 2). This test drives the final level
    directly (as test_level_0_breach_exits_only_the_bigger_loser_leg
    already covers the ratcheting-by-one-level behavior)."""
    strategy = _strategy(quantity=1)
    _enter(strategy, entry_price=100.0)  # fires at ltp=40 for both legs
    strategy._risk_level_index = len(RISK_LOSS_LEVELS) - 1  # already at the final level

    ce_ltp, pe_ltp = 40.0 + 1100.0, 40.0 + 1000.0  # combined unrealized = 2100 >= 2000
    intents = _feed(strategy, ce_ltp, pe_ltp)
    assert {i.symbol for i in intents} == {CE, PE}
    assert all(i.side == OrderSide.BUY for i in intents)
    assert strategy._day_stopped is True
    # Day-stopped: no further entries or ladder checks even with fresh data.
    more = _feed(strategy, 1.0, 1.0)
    assert more == []


def test_risk_loss_levels_match_the_legacy_config_exactly():
    assert RISK_LOSS_LEVELS == (650.0, 1300.0, 2000.0)


# --------------------------------------------------------------------------- #
# Structural safety
# --------------------------------------------------------------------------- #
def test_strategy_module_never_imports_a_broker_or_live_authorization():
    import inspect
    import trading.common.strategies.combined_vwap_nifty as mod

    source = inspect.getsource(mod)
    for forbidden in (
        "AngelOneBroker", "DhanBroker", "ICICIBreezeBroker", "SmartConnect", "SmartAPI",
        "breeze_connect", "BreezeConnect", "place_order", "modify_order", "cancel_order",
    ):
        assert forbidden not in source, forbidden
    assert "live_authorization" not in source.lower()
    assert "trading.algos" not in source


# --------------------------------------------------------------------------- #
# Full pipe: Market Data -> Strategy -> OrderIntent -> Engine -> Shadow/Paper
# --------------------------------------------------------------------------- #
def _full_stack(account_id="ACC1", risk_limits=None):
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id=account_id, account_name="A", broker_id="paper", execution_mode=ExecutionMode.SHADOW),
        broker_client=PaperBroker(),
    )
    registry = StrategyRegistry()
    strategy = CombinedVwapNiftyStrategy(account_id=account_id, ce_instrument=CE, pe_instrument=PE)
    registry.register(strategy)
    assignment = StrategyAssignment(manager)
    assignment.assign(STRATEGY_ID, account_id)
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)
    risk_manager = RiskManager(assignment, limits=risk_limits)
    kill_switch = CentralKillSwitch()
    source = FixedMarketDataSource()
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch, market_data_source=source,
    )
    return manager, registry, assignment, runtime, source, strategy


def test_end_to_end_entry_reaches_a_simulated_shadow_execution():
    manager, registry, assignment, runtime, source, strategy = _full_stack()
    source.set_quote(CE, _opt_quote(CE, 150.0))
    source.set_quote(PE, _opt_quote(PE, 150.0))
    runtime.run_once(STRATEGY_ID)
    source.set_quote(CE, _opt_quote(CE, 200.0))
    source.set_quote(PE, _opt_quote(PE, 200.0))
    runtime.run_once(STRATEGY_ID)
    source.set_quote(CE, _opt_quote(CE, 90.0))
    source.set_quote(PE, _opt_quote(PE, 90.0))
    result = runtime.run_once(STRATEGY_ID)

    assert result.intents_generated == 2
    assert len(result.executions) == 2
    assert all(e.success for e in result.executions)


def test_end_to_end_no_signal_produces_zero_executions():
    manager, registry, assignment, runtime, source, strategy = _full_stack()
    source.set_quote(CE, _opt_quote(CE, 100.0))
    source.set_quote(PE, _opt_quote(PE, 100.0))
    result = runtime.run_once(STRATEGY_ID)
    assert result.intents_generated == 0
    assert result.executions == []


def test_end_to_end_never_calls_a_real_broker_placeorder():
    class _RecordingBroker(PaperBroker):
        def __init__(self):
            super().__init__()
            self.place_order_calls = 0

        def place_order(self, *a, **kw):
            self.place_order_calls += 1
            return super().place_order(*a, **kw)

    manager = BrokerManager()
    recording = _RecordingBroker()
    manager.register_account(
        TradingAccount(account_id="ACC1", account_name="A", broker_id="paper", execution_mode=ExecutionMode.SHADOW),
        broker_client=recording,
    )
    registry = StrategyRegistry()
    strategy = CombinedVwapNiftyStrategy(account_id="ACC1", ce_instrument=CE, pe_instrument=PE)
    registry.register(strategy)
    assignment = StrategyAssignment(manager)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)
    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    source = FixedMarketDataSource()
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch, market_data_source=source,
    )
    for ce_ltp, pe_ltp in ((150.0, 150.0), (200.0, 200.0), (90.0, 90.0)):
        source.set_quote(CE, _opt_quote(CE, ce_ltp))
        source.set_quote(PE, _opt_quote(PE, pe_ltp))
        result = runtime.run_once(STRATEGY_ID)

    assert result.intents_generated == 2
    assert recording.place_order_calls == 0  # dry_run short-circuits before it, every time


# --------------------------------------------------------------------------- #
# RiskManager remains authoritative (Part 14)
# --------------------------------------------------------------------------- #
def test_risk_manager_rejects_when_quantity_exceeds_configured_limit():
    manager, registry, assignment, runtime, source, strategy = _full_stack(
        risk_limits=RiskLimits(max_order_quantity=10),  # strategy's real quantity is 65 -- must be rejected
    )
    for ce_ltp, pe_ltp in ((150.0, 150.0), (200.0, 200.0), (90.0, 90.0)):
        source.set_quote(CE, _opt_quote(CE, ce_ltp))
        source.set_quote(PE, _opt_quote(PE, pe_ltp))
        result = runtime.run_once(STRATEGY_ID)

    assert result.intents_generated == 2
    assert all(not e.success for e in result.executions)


def test_risk_manager_allows_a_valid_intent_in_shadow_mode():
    manager, registry, assignment, runtime, source, strategy = _full_stack(
        risk_limits=RiskLimits(max_order_quantity=100),
    )
    for ce_ltp, pe_ltp in ((150.0, 150.0), (200.0, 200.0), (90.0, 90.0)):
        source.set_quote(CE, _opt_quote(CE, ce_ltp))
        source.set_quote(PE, _opt_quote(PE, pe_ltp))
        result = runtime.run_once(STRATEGY_ID)

    assert all(e.success for e in result.executions)


# --------------------------------------------------------------------------- #
# Idempotency (reusing the existing store -- Part 15)
# --------------------------------------------------------------------------- #
def test_repeated_run_once_with_identical_market_data_is_idempotent_end_to_end():
    manager, registry, assignment, runtime, source, strategy = _full_stack()
    source.set_quote(CE, _opt_quote(CE, 150.0))
    source.set_quote(PE, _opt_quote(PE, 150.0))
    runtime.run_once(STRATEGY_ID)
    source.set_quote(CE, _opt_quote(CE, 200.0))
    source.set_quote(PE, _opt_quote(PE, 200.0))
    runtime.run_once(STRATEGY_ID)
    source.set_quote(CE, _opt_quote(CE, 90.0))
    source.set_quote(PE, _opt_quote(PE, 90.0))
    first = runtime.run_once(STRATEGY_ID)
    second = runtime.run_once(STRATEGY_ID)  # identical data, strategy already in-position -- no re-entry

    assert first.intents_generated == 2
    assert second.intents_generated == 0


# --------------------------------------------------------------------------- #
# Multi-account isolation and concurrency (Part 17)
# --------------------------------------------------------------------------- #
def test_two_instances_on_different_accounts_never_cross_contaminate():
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id="ACC_A", account_name="A", broker_id="paper", execution_mode=ExecutionMode.SHADOW),
        broker_client=PaperBroker(),
    )
    manager.register_account(
        TradingAccount(account_id="ACC_B", account_name="B", broker_id="paper", execution_mode=ExecutionMode.SHADOW),
        broker_client=PaperBroker(),
    )
    # Two instances of the SAME strategy_id can't both be registered in one
    # StrategyRegistry (Phase 10 registry is keyed by strategy_id) -- this
    # test instead proves per-INSTANCE isolation directly, which is what
    # actually matters: two independent strategy objects never share state.
    strat_a = CombinedVwapNiftyStrategy(account_id="ACC_A", ce_instrument="A_CE", pe_instrument="A_PE")
    strat_b = CombinedVwapNiftyStrategy(account_id="ACC_B", ce_instrument="B_CE", pe_instrument="B_PE")

    for s in (strat_a, strat_b):
        s.enable()
        s.start()

    a_intents = []
    for ce_ltp, pe_ltp in ((150.0, 150.0), (200.0, 200.0), (90.0, 90.0)):
        a_intents = strat_a.generate_order_intents({"A_CE": _opt_quote("A_CE", ce_ltp), "A_PE": _opt_quote("A_PE", pe_ltp)})

    b_intents = strat_b.generate_order_intents({"B_CE": _opt_quote("B_CE", 100.0), "B_PE": _opt_quote("B_PE", 100.0)})

    assert len(a_intents) == 2
    assert all(i.account_id == "ACC_A" for i in a_intents)
    assert b_intents == []  # strat_b's own independent state was never armed -- unaffected by strat_a's history
    assert all(i.account_id != "ACC_B" for i in a_intents)


def test_concurrent_evaluation_of_two_independent_instances_is_safe():
    strat_a = CombinedVwapNiftyStrategy(account_id="ACC_A", ce_instrument="A_CE", pe_instrument="A_PE")
    strat_b = CombinedVwapNiftyStrategy(account_id="ACC_B", ce_instrument="B_CE", pe_instrument="B_PE")
    for s in (strat_a, strat_b):
        s.enable()
        s.start()

    results = {}

    def run_a():
        for ce_ltp, pe_ltp in ((150.0, 150.0), (200.0, 200.0), (90.0, 90.0)):
            results["a"] = strat_a.generate_order_intents(
                {"A_CE": _opt_quote("A_CE", ce_ltp), "A_PE": _opt_quote("A_PE", pe_ltp)},
            )

    def run_b():
        results["b"] = strat_b.generate_order_intents(
            {"B_CE": _opt_quote("B_CE", 100.0), "B_PE": _opt_quote("B_PE", 100.0)},
        )

    threads = [threading.Thread(target=run_a), threading.Thread(target=run_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results["a"]) == 2
    assert results["b"] == []
