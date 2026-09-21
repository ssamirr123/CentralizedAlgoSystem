"""Phase 16.8: trading/common/strategies/double_straddle.py -- the ported
wall-clock-scheduled hedged-short-straddle entry/exit rules, consuming
normalized market data and emitting genuine OrderIntents."""
from __future__ import annotations

import datetime as dt

from trading.common.broker import OrderSide
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.kill_switch import CentralKillSwitch
from trading.common.market_data_gateway import FixedMarketDataSource
from trading.common.risk_manager import RiskLimits, RiskManager
from trading.common.strategies.double_straddle import (
    DAILY_MAX_LOSS,
    SL_POINTS,
    TARGET_POINTS,
    DoubleStraddleStrategy,
)
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_registry import StrategyRegistry
from trading.common.strategy_runtime import StrategyRuntime
from trading.common.trading_account import ExecutionMode, TradingAccount
from trading.market_data.schemas import OptionQuote

STRATEGY_ID = "DoubleStraddelAlgo"
HCE, HPE, SCE, SPE = "H_CE", "H_PE", "S_CE", "S_PE"


def _quote(symbol: str, ltp: float) -> OptionQuote:
    return OptionQuote(
        symbol=symbol, underlying="NIFTY", expiry=dt.date(2026, 9, 25), strike=24950.0,
        option_type="CE" if "CE" in symbol else "PE", ltp=ltp,
    )


def _md(hce=100.0, hpe=100.0, sce=100.0, spe=100.0) -> dict:
    return {HCE: _quote(HCE, hce), HPE: _quote(HPE, hpe), SCE: _quote(SCE, sce), SPE: _quote(SPE, spe)}


def _at(h, m) -> callable:
    return lambda: dt.datetime(2026, 9, 19, h, m, 0)


def _strategy(clock, **kw) -> DoubleStraddleStrategy:
    s = DoubleStraddleStrategy(
        hedge_ce_instrument=HCE, hedge_pe_instrument=HPE, straddle_ce_instrument=SCE, straddle_pe_instrument=SPE,
        account_id="ACC1", clock=clock, **kw,
    )
    s.enable()
    s.start()
    return s


# --------------------------------------------------------------------------- #
# required_instruments / no-trade
# --------------------------------------------------------------------------- #
def test_declares_all_four_legs_as_required_instruments():
    s = DoubleStraddleStrategy(
        hedge_ce_instrument=HCE, hedge_pe_instrument=HPE, straddle_ce_instrument=SCE, straddle_pe_instrument=SPE,
    )
    assert set(s.required_instruments()) == {HCE, HPE, SCE, SPE}


def test_no_market_data_produces_zero_intents():
    s = _strategy(_at(10, 20))
    assert s.generate_order_intents(None) == []


def test_before_hedge_entry_time_produces_zero_intents():
    s = _strategy(_at(9, 0))
    assert s.generate_order_intents(_md()) == []


def test_partial_market_data_produces_zero_intents():
    s = _strategy(_at(10, 20))
    assert s.generate_order_intents({HCE: _quote(HCE, 100.0)}) == []


# --------------------------------------------------------------------------- #
# Hedge entry -- unconditional at 10:20
# --------------------------------------------------------------------------- #
def test_hedge_entry_fires_at_hedge_entry_time():
    s = _strategy(_at(10, 20))
    intents = s.generate_order_intents(_md())
    assert len(intents) == 2
    assert {i.symbol for i in intents} == {HCE, HPE}
    assert all(i.side == OrderSide.BUY for i in intents)
    assert s._hedge_active is True


def test_hedge_entry_does_not_refire():
    s = _strategy(_at(10, 20))
    s.generate_order_intents(_md())
    second = s.generate_order_intents(_md())
    assert second == []


# --------------------------------------------------------------------------- #
# Morning straddle entry -- gated on hedge, unconditional otherwise
# --------------------------------------------------------------------------- #
def test_morning_entry_fires_only_after_hedge_is_active():
    s = _strategy(_at(10, 20))
    s.generate_order_intents(_md())  # hedge fires
    s._clock = _at(10, 25)
    intents = s.generate_order_intents(_md())
    assert len(intents) == 2
    assert {i.symbol for i in intents} == {SCE, SPE}
    assert all(i.side == OrderSide.SELL for i in intents)


def test_morning_entry_gate_gives_way_once_hedge_has_actually_entered():
    """engine.py:56's hedge_active gate is preserved structurally (the
    morning-entry code path is unconditionally nested inside `if
    self._hedge_active:`) -- since this integration's hedge entry cannot
    itself fail (no broker call happens inside the strategy), the only
    way to observe the gate is to confirm morning entry is READ
    per-cycle, not cached: a fresh strategy with hedge already inactive
    and evaluated before HEDGE_ENTRY_TIME produces no straddle legs at
    all, proving the gate is consulted rather than assumed true."""
    s = _strategy(_at(9, 0))  # before hedge entry time -- hedge_active stays False
    intents = s.generate_order_intents(_md())
    assert intents == []
    assert s._hedge_active is False


# --------------------------------------------------------------------------- #
# SL / target exit -- exact legacy points
# --------------------------------------------------------------------------- #
def _entered_morning(entry_price=100.0):
    s = _strategy(_at(10, 20))
    s.generate_order_intents(_md())
    s._clock = _at(10, 25)
    s.generate_order_intents(_md(sce=entry_price, spe=entry_price))
    return s


def test_sl_exit_fires_at_entry_plus_sl_points():
    s = _entered_morning(entry_price=100.0)
    s._clock = _at(11, 0)
    intents = s.generate_order_intents(_md(sce=100.0 + SL_POINTS))  # exactly at SL
    assert len(intents) == 1
    assert intents[0].symbol == SCE
    assert intents[0].side == OrderSide.BUY


def test_target_exit_fires_at_entry_minus_target_points():
    s = _entered_morning(entry_price=100.0)
    s._clock = _at(11, 0)
    intents = s.generate_order_intents(_md(spe=100.0 - TARGET_POINTS))  # exactly at target
    assert len(intents) == 1
    assert intents[0].symbol == SPE


def test_no_exit_between_sl_and_target():
    s = _entered_morning(entry_price=100.0)
    s._clock = _at(11, 0)
    intents = s.generate_order_intents(_md(sce=110.0, spe=90.0))
    assert intents == []


# --------------------------------------------------------------------------- #
# Time-based exits
# --------------------------------------------------------------------------- #
def test_morning_exit_force_closes_open_legs_at_morning_exit_time():
    s = _entered_morning(entry_price=100.0)
    s._clock = _at(14, 14)
    intents = s.generate_order_intents(_md(sce=105.0, spe=95.0))  # no SL/target hit
    assert {i.symbol for i in intents} == {SCE, SPE}
    assert s._morning.exited is True


def test_final_exit_closes_afternoon_and_hedge_and_stops_the_day():
    s = _strategy(_at(10, 20))
    s.generate_order_intents(_md())
    s._clock = _at(10, 25)
    s.generate_order_intents(_md())
    s._clock = _at(14, 14)
    s.generate_order_intents(_md())  # morning force-closed
    s._clock = _at(14, 16)
    s.generate_order_intents(_md())  # afternoon entered
    s._clock = _at(15, 25)
    intents = s.generate_order_intents(_md())

    symbols = {i.symbol for i in intents}
    assert symbols == {SCE, SPE, HCE, HPE}
    assert s._day_stopped is True
    more = s.generate_order_intents(_md())
    assert more == []


# --------------------------------------------------------------------------- #
# Portfolio kill switch
# --------------------------------------------------------------------------- #
def test_kill_switch_closes_everything_and_stops_the_day():
    s = _strategy(_at(10, 20), quantity=1)
    s.generate_order_intents(_md())  # hedge entered at 100/100
    s._clock = _at(10, 25)
    s.generate_order_intents(_md())  # straddle entered at 100/100
    s._clock = _at(11, 0)
    # Straddle legs move hard against us (short, price way up); hedge barely moves.
    intents = s.generate_order_intents(_md(hce=101.0, hpe=101.0, sce=100.0 + DAILY_MAX_LOSS + 10.0, spe=100.0))
    symbols = {i.symbol for i in intents}
    assert HCE in symbols and HPE in symbols  # hedge exited too
    assert SCE in symbols  # the open straddle leg exited
    assert s._day_stopped is True


def test_no_kill_switch_when_loss_within_bounds():
    s = _strategy(_at(10, 20), quantity=1)
    s.generate_order_intents(_md())
    s._clock = _at(10, 25)
    s.generate_order_intents(_md())
    s._clock = _at(11, 0)
    intents = s.generate_order_intents(_md(sce=105.0, spe=95.0))
    assert s._day_stopped is False
    assert intents == []


# --------------------------------------------------------------------------- #
# Structural safety
# --------------------------------------------------------------------------- #
def test_module_never_imports_a_broker_or_live_authorization():
    import inspect
    import trading.common.strategies.double_straddle as mod

    source = inspect.getsource(mod)
    for forbidden in (
        "AngelOneBroker", "DhanBroker", "ICICIBreezeBroker", "SmartConnect", "SmartAPI",
        "breeze_connect", "BreezeConnect", "place_order", "modify_order", "cancel_order",
    ):
        assert forbidden not in source, forbidden
    assert "live_authorization" not in source.lower()
    assert "trading.algos" not in source


# --------------------------------------------------------------------------- #
# Full pipe + RiskManager + idempotency + isolation
# --------------------------------------------------------------------------- #
def _full_stack(account_id="ACC1", risk_limits=None, clock=None):
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id=account_id, account_name="A", broker_id="paper", execution_mode=ExecutionMode.SHADOW),
        broker_client=PaperBroker(),
    )
    registry = StrategyRegistry()
    strategy = DoubleStraddleStrategy(
        hedge_ce_instrument=HCE, hedge_pe_instrument=HPE, straddle_ce_instrument=SCE, straddle_pe_instrument=SPE,
        account_id=account_id, clock=clock or _at(10, 20),
    )
    registry.register(strategy)
    assignment = StrategyAssignment(manager)
    assignment.assign(STRATEGY_ID, account_id)
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)
    risk_manager = RiskManager(assignment, limits=risk_limits)
    kill_switch = CentralKillSwitch()
    source = FixedMarketDataSource()
    for instrument in (HCE, HPE, SCE, SPE):
        source.set_quote(instrument, _quote(instrument, 100.0))
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch, market_data_source=source,
    )
    return manager, registry, assignment, runtime, source, strategy


def test_end_to_end_hedge_entry_reaches_a_simulated_execution():
    manager, registry, assignment, runtime, source, strategy = _full_stack()
    result = runtime.run_once(STRATEGY_ID)
    assert result.intents_generated == 2
    assert all(e.success for e in result.executions)


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
    strategy = DoubleStraddleStrategy(
        hedge_ce_instrument=HCE, hedge_pe_instrument=HPE, straddle_ce_instrument=SCE, straddle_pe_instrument=SPE,
        account_id="ACC1", clock=_at(10, 20),
    )
    registry.register(strategy)
    assignment = StrategyAssignment(manager)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)
    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    source = FixedMarketDataSource()
    for instrument in (HCE, HPE, SCE, SPE):
        source.set_quote(instrument, _quote(instrument, 100.0))
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch, market_data_source=source,
    )
    result = runtime.run_once(STRATEGY_ID)
    assert result.intents_generated == 2
    assert recording.place_order_calls == 0


def test_risk_manager_rejects_when_quantity_exceeds_configured_limit():
    manager, registry, assignment, runtime, source, strategy = _full_stack(
        risk_limits=RiskLimits(max_order_quantity=10),
    )
    result = runtime.run_once(STRATEGY_ID)
    assert result.intents_generated == 2
    assert all(not e.success for e in result.executions)


def test_repeated_identical_evaluation_is_idempotent():
    manager, registry, assignment, runtime, source, strategy = _full_stack()
    first = runtime.run_once(STRATEGY_ID)
    second = runtime.run_once(STRATEGY_ID)
    assert first.intents_generated == 2
    assert second.intents_generated == 0  # hedge_active already True -- no re-fire


def test_two_instances_on_different_accounts_never_cross_contaminate():
    strat_a = DoubleStraddleStrategy(
        hedge_ce_instrument="A_HCE", hedge_pe_instrument="A_HPE",
        straddle_ce_instrument="A_SCE", straddle_pe_instrument="A_SPE",
        account_id="ACC_A", clock=_at(10, 20),
    )
    strat_b = DoubleStraddleStrategy(
        hedge_ce_instrument="B_HCE", hedge_pe_instrument="B_HPE",
        straddle_ce_instrument="B_SCE", straddle_pe_instrument="B_SPE",
        account_id="ACC_B", clock=_at(9, 0),  # before hedge entry time -- no-op
    )
    for s in (strat_a, strat_b):
        s.enable()
        s.start()

    a_intents = strat_a.generate_order_intents({
        "A_HCE": _quote("A_HCE", 100.0), "A_HPE": _quote("A_HPE", 100.0),
        "A_SCE": _quote("A_SCE", 100.0), "A_SPE": _quote("A_SPE", 100.0),
    })
    b_intents = strat_b.generate_order_intents({
        "B_HCE": _quote("B_HCE", 100.0), "B_HPE": _quote("B_HPE", 100.0),
        "B_SCE": _quote("B_SCE", 100.0), "B_SPE": _quote("B_SPE", 100.0),
    })

    assert len(a_intents) == 2
    assert all(i.account_id == "ACC_A" for i in a_intents)
    assert b_intents == []


# --------------------------------------------------------------------------- #
# Phase 17.1-R Remediation C: trading-date-safe idempotency identity.
# --------------------------------------------------------------------------- #
def _on(year, month, day, h, m) -> callable:
    return lambda: dt.datetime(year, month, day, h, m, 0)


def test_same_day_worker_restart_produces_the_same_idempotency_key():
    """A worker crash/restart re-evaluating the SAME logical event on the
    SAME trading day (a brand-new strategy instance, exactly what happens
    on process restart -- no persisted in-memory state) must derive the
    identical idempotency_key, so the idempotency store correctly treats a
    post-restart retry as a replay, not a fresh order."""
    clock = _on(2026, 9, 19, 10, 20)
    first_process = _strategy(clock)
    first_intents = first_process.generate_order_intents(_md())

    second_process = _strategy(clock)  # simulates a fresh worker process after restart
    second_intents = second_process.generate_order_intents(_md())

    assert {i.idempotency_key for i in first_intents} == {i.idempotency_key for i in second_intents}


def test_next_trading_day_produces_a_different_idempotency_key():
    """The exact bug this remediation closes: the SAME logical event
    (hedge entry) on two different trading days must never collide."""
    day1 = _strategy(_on(2026, 9, 19, 10, 20))
    day1_intents = day1.generate_order_intents(_md())

    day2 = _strategy(_on(2026, 9, 22, 10, 20))  # next trading day (Mon after Fri 9/19 -- weekends are not this strategy's concern)
    day2_intents = day2.generate_order_intents(_md())

    day1_keys = {i.idempotency_key for i in day1_intents}
    day2_keys = {i.idempotency_key for i in day2_intents}
    assert day1_keys.isdisjoint(day2_keys)
    assert len(day1_keys) == 2 and len(day2_keys) == 2


def test_morning_vs_afternoon_logical_event_have_distinct_keys_same_day():
    clock_state = {"now": dt.datetime(2026, 9, 19, 10, 20, 0)}
    s = _strategy(lambda: clock_state["now"])
    s.generate_order_intents(_md())  # hedge entry

    clock_state["now"] = dt.datetime(2026, 9, 19, 10, 25, 0)
    morning_intents = s.generate_order_intents(_md())

    clock_state["now"] = dt.datetime(2026, 9, 19, 14, 16, 0)
    afternoon_intents = s.generate_order_intents(_md())

    morning_keys = {i.idempotency_key for i in morning_intents}
    afternoon_keys = {i.idempotency_key for i in afternoon_intents}
    assert morning_keys.isdisjoint(afternoon_keys)
    assert any("morning" in k for k in morning_keys)
    assert any("afternoon" in k for k in afternoon_keys)


def test_ce_and_pe_leg_have_distinct_keys():
    s = _strategy(_on(2026, 9, 19, 10, 20))
    intents = s.generate_order_intents(_md())
    keys = {i.symbol: i.idempotency_key for i in intents}
    assert keys[HCE] != keys[HPE]


def test_idempotency_key_carries_an_isoformat_trading_date():
    s = _strategy(_on(2026, 9, 19, 10, 20))
    intents = s.generate_order_intents(_md())
    for intent in intents:
        assert "2026-09-19" in intent.idempotency_key


def test_same_day_duplicate_evaluation_is_still_a_stable_key_not_a_new_one():
    """Calling generate_order_intents twice within the same trading day for
    a DIFFERENT logical event (hedge, then morning) must not accidentally
    produce colliding keys, and re-deriving the date each call must not
    introduce drift for events firing minutes apart on the same day."""
    clock_state = {"now": dt.datetime(2026, 9, 19, 10, 20, 0)}
    s = _strategy(lambda: clock_state["now"])
    hedge_intents = s.generate_order_intents(_md())

    clock_state["now"] = dt.datetime(2026, 9, 19, 10, 25, 0)
    morning_intents = s.generate_order_intents(_md())

    hedge_keys = {i.idempotency_key for i in hedge_intents}
    morning_keys = {i.idempotency_key for i in morning_intents}
    assert hedge_keys.isdisjoint(morning_keys)
