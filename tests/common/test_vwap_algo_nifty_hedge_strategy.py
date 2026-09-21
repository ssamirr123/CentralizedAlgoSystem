"""Phase 16.8: trading/common/strategies/vwap_algo_nifty_hedge.py -- the
ported VWAP-cross arm/breakdown-fire entry rule and stop-loss/time-exit
logic, consuming normalized market data and emitting genuine
OrderIntents. One instance = one side (CE or PE), matching the legacy
algo's own two-independent-instances design."""
from __future__ import annotations

import datetime as dt

from trading.common.broker import OrderSide
from trading.common.broker_manager import BrokerManager
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.kill_switch import CentralKillSwitch
from trading.common.market_data_gateway import FixedMarketDataSource
from trading.common.risk_manager import RiskLimits, RiskManager
from trading.common.strategies.vwap_algo_nifty_hedge import (
    MAX_ENTRIES,
    VwapAlgoNiftyHedgeStrategy,
)
from trading.common.strategy_assignment import StrategyAssignment
from trading.common.strategy_registry import StrategyRegistry
from trading.common.strategy_runtime import StrategyRuntime
from trading.common.trading_account import ExecutionMode, TradingAccount
from trading.market_data.schemas import OptionQuote

STRATEGY_ID = "Vwap_Algo_Nifty_hedge"
OPT, HEDGE = "OPT_CE", "OPT_HEDGE_CE"


def _quote(symbol: str, ltp: float) -> OptionQuote:
    return OptionQuote(
        symbol=symbol, underlying="NIFTY", expiry=dt.date(2026, 9, 25), strike=24950.0, option_type="CE", ltp=ltp,
    )


def _md(opt=100.0, hedge=100.0) -> dict:
    return {OPT: _quote(OPT, opt), HEDGE: _quote(HEDGE, hedge)}


def _at(h, m) -> callable:
    return lambda: dt.datetime(2026, 9, 19, h, m, 0)


def _strategy(clock=None, **kw) -> VwapAlgoNiftyHedgeStrategy:
    s = VwapAlgoNiftyHedgeStrategy(
        option_instrument=OPT, hedge_instrument=HEDGE, account_id="ACC1", clock=clock or _at(9, 30), **kw,
    )
    s.enable()
    s.start()
    return s


# --------------------------------------------------------------------------- #
# required_instruments / no-trade
# --------------------------------------------------------------------------- #
def test_declares_option_and_hedge_as_required_instruments():
    s = VwapAlgoNiftyHedgeStrategy(option_instrument=OPT, hedge_instrument=HEDGE)
    assert set(s.required_instruments()) == {OPT, HEDGE}


def test_no_market_data_produces_zero_intents():
    s = _strategy()
    assert s.generate_order_intents(None) == []


def test_partial_market_data_produces_zero_intents():
    s = _strategy()
    assert s.generate_order_intents({OPT: _quote(OPT, 100.0)}) == []


# --------------------------------------------------------------------------- #
# Hedge entry -- unconditional on first evaluation
# --------------------------------------------------------------------------- #
def test_hedge_enters_unconditionally_on_first_call():
    s = _strategy()
    intents = s.generate_order_intents(_md())
    assert any(i.symbol == HEDGE and i.side == OrderSide.BUY for i in intents)
    assert s._hedge_entered is True


def test_hedge_does_not_reenter_on_subsequent_calls():
    s = _strategy()
    s.generate_order_intents(_md())
    second = s.generate_order_intents(_md(opt=100.0))
    assert all(i.symbol != HEDGE for i in second)


# --------------------------------------------------------------------------- #
# Arm / cancel / fire -- the ported VWAP-cross rule
# --------------------------------------------------------------------------- #
def test_never_armed_condition_produces_zero_entries():
    s = _strategy()
    s.generate_order_intents(_md(opt=100.0))  # sample 1: vwap==close, no arm
    intents = s.generate_order_intents(_md(opt=100.0))  # still equal -- never below own average
    assert all(i.symbol != OPT for i in intents)
    assert s._istriggered is False


def test_arm_then_fire_generates_a_short_entry():
    s = _strategy()
    s.generate_order_intents(_md(opt=100.0))  # vwap=100
    s.generate_order_intents(_md(opt=150.0))  # vwap=125, close(150)>vwap -- no arm yet
    armed = s.generate_order_intents(_md(opt=90.0))  # vwap=113.3, close(90)<vwap -- ARM at 90
    assert all(i.symbol != OPT for i in armed)
    assert s._istriggered is True
    assert s._triggerlow == 90.0

    fired = s.generate_order_intents(_md(opt=80.0))  # close(80) < triggerlow(90) -- FIRE
    entry = [i for i in fired if i.symbol == OPT]
    assert len(entry) == 1
    assert entry[0].side == OrderSide.SELL
    assert s._isintrade is True
    assert s._entry_price == 80.0
    assert s._stoploss == 90.0  # spread=triggerhigh-close=10 -> stoploss=triggerhigh


def test_cancel_arm_when_price_recovers_above_vwap():
    s = _strategy()
    s.generate_order_intents(_md(opt=100.0))
    s.generate_order_intents(_md(opt=150.0))
    s.generate_order_intents(_md(opt=90.0))  # armed
    assert s._istriggered is True
    s.generate_order_intents(_md(opt=200.0))  # vwap rises but 200 still > new vwap -- cancels
    assert s._istriggered is False
    assert s._triggerlow is None


# --------------------------------------------------------------------------- #
# Stop-loss exit
# --------------------------------------------------------------------------- #
def _entered() -> VwapAlgoNiftyHedgeStrategy:
    s = _strategy()
    s.generate_order_intents(_md(opt=100.0))
    s.generate_order_intents(_md(opt=150.0))
    s.generate_order_intents(_md(opt=90.0))
    s.generate_order_intents(_md(opt=80.0))  # fired, entry=80, stoploss=90
    return s


def test_stoploss_exit_fires_when_price_reaches_stoploss():
    s = _entered()
    intents = s.generate_order_intents(_md(opt=95.0))  # >= stoploss(90)
    exits = [i for i in intents if i.symbol == OPT]
    assert len(exits) == 1
    assert exits[0].side == OrderSide.BUY
    assert s._isintrade is False


def test_no_exit_below_stoploss():
    s = _entered()
    intents = s.generate_order_intents(_md(opt=85.0))  # below stoploss(90)
    assert all(i.symbol != OPT for i in intents)
    assert s._isintrade is True


# --------------------------------------------------------------------------- #
# Timeup / final exit
# --------------------------------------------------------------------------- #
def test_no_new_arm_after_timeup():
    s = _strategy(clock=_at(14, 31))
    s.generate_order_intents(_md(opt=100.0))
    intents = s.generate_order_intents(_md(opt=50.0))  # would otherwise arm
    assert s._istriggered is False
    assert all(i.symbol != OPT for i in intents)


def test_final_exit_closes_open_position_and_hedge_and_stops_the_day():
    s = _entered()  # in a trade, entry=80
    s._clock = _at(15, 25)
    intents = s.generate_order_intents(_md(opt=82.0))
    symbols = {i.symbol for i in intents}
    assert OPT in symbols and HEDGE in symbols
    assert s._day_done is True
    more = s.generate_order_intents(_md(opt=1.0))
    assert more == []


def test_final_exit_still_exits_hedge_even_with_no_open_position():
    s = _strategy()
    s.generate_order_intents(_md())  # hedge entry only
    s._clock = _at(15, 25)
    intents = s.generate_order_intents(_md())
    assert any(i.symbol == HEDGE and i.side == OrderSide.SELL for i in intents)
    assert not any(i.symbol == OPT for i in intents)  # never in a trade -- nothing to close


# --------------------------------------------------------------------------- #
# Max entries cap
# --------------------------------------------------------------------------- #
def test_max_entries_cap_blocks_a_further_entry():
    s = _strategy()
    s._entry_count = MAX_ENTRIES
    s._istriggered = True
    s._triggerlow = 90.0
    s._triggerhigh = 90.0
    intents = s.generate_order_intents(_md(opt=80.0))
    assert all(i.symbol != OPT for i in intents)
    assert s._isintrade is False


# --------------------------------------------------------------------------- #
# Structural safety
# --------------------------------------------------------------------------- #
def test_module_never_imports_a_broker_or_live_authorization():
    import inspect
    import trading.common.strategies.vwap_algo_nifty_hedge as mod

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
def _full_stack(account_id="ACC1", risk_limits=None):
    manager = BrokerManager()
    manager.register_account(
        TradingAccount(account_id=account_id, account_name="A", broker_id="paper", execution_mode=ExecutionMode.SHADOW),
        broker_client=PaperBroker(),
    )
    registry = StrategyRegistry()
    strategy = VwapAlgoNiftyHedgeStrategy(
        option_instrument=OPT, hedge_instrument=HEDGE, account_id=account_id, clock=_at(9, 30),
    )
    registry.register(strategy)
    assignment = StrategyAssignment(manager)
    assignment.assign(STRATEGY_ID, account_id)
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)
    risk_manager = RiskManager(assignment, limits=risk_limits)
    kill_switch = CentralKillSwitch()
    source = FixedMarketDataSource()
    source.set_quote(OPT, _quote(OPT, 100.0))
    source.set_quote(HEDGE, _quote(HEDGE, 100.0))
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch, market_data_source=source,
    )
    return manager, registry, assignment, runtime, source, strategy


def test_end_to_end_hedge_entry_reaches_a_simulated_execution():
    manager, registry, assignment, runtime, source, strategy = _full_stack()
    result = runtime.run_once(STRATEGY_ID)
    assert result.intents_generated == 1
    assert all(e.success for e in result.executions)


def test_end_to_end_arm_fire_reaches_a_simulated_execution():
    manager, registry, assignment, runtime, source, strategy = _full_stack()
    runtime.run_once(STRATEGY_ID)  # hedge
    source.set_quote(OPT, _quote(OPT, 150.0))
    runtime.run_once(STRATEGY_ID)
    source.set_quote(OPT, _quote(OPT, 90.0))
    runtime.run_once(STRATEGY_ID)  # armed
    source.set_quote(OPT, _quote(OPT, 80.0))
    result = runtime.run_once(STRATEGY_ID)  # fired

    assert result.intents_generated == 1
    assert result.executions[0].success is True


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
    strategy = VwapAlgoNiftyHedgeStrategy(option_instrument=OPT, hedge_instrument=HEDGE, account_id="ACC1", clock=_at(9, 30))
    registry.register(strategy)
    assignment = StrategyAssignment(manager)
    assignment.assign(STRATEGY_ID, "ACC1")
    registry.enable(STRATEGY_ID)
    registry.start(STRATEGY_ID)
    risk_manager = RiskManager(assignment)
    kill_switch = CentralKillSwitch()
    source = FixedMarketDataSource()
    source.set_quote(OPT, _quote(OPT, 100.0))
    source.set_quote(HEDGE, _quote(HEDGE, 100.0))
    runtime = StrategyRuntime(
        strategy_registry=registry, strategy_assignment=assignment, broker_manager=manager,
        risk_manager=risk_manager, kill_switch=kill_switch, market_data_source=source,
    )
    result = runtime.run_once(STRATEGY_ID)
    assert result.intents_generated == 1
    assert recording.place_order_calls == 0


def test_risk_manager_rejects_when_quantity_exceeds_configured_limit():
    manager, registry, assignment, runtime, source, strategy = _full_stack(
        risk_limits=RiskLimits(max_order_quantity=10),
    )
    result = runtime.run_once(STRATEGY_ID)
    assert result.intents_generated == 1
    assert all(not e.success for e in result.executions)


def test_repeated_identical_evaluation_is_idempotent():
    manager, registry, assignment, runtime, source, strategy = _full_stack()
    first = runtime.run_once(STRATEGY_ID)
    second = runtime.run_once(STRATEGY_ID)
    assert first.intents_generated == 1
    assert second.intents_generated == 0  # hedge already entered -- no re-fire


def test_two_instances_on_different_accounts_never_cross_contaminate():
    strat_a = VwapAlgoNiftyHedgeStrategy(
        option_instrument="A_OPT", hedge_instrument="A_HEDGE", account_id="ACC_A", clock=_at(9, 30),
    )
    strat_b = VwapAlgoNiftyHedgeStrategy(
        option_instrument="B_OPT", hedge_instrument="B_HEDGE", account_id="ACC_B", clock=_at(9, 30),
    )
    for s in (strat_a, strat_b):
        s.enable()
        s.start()

    a_intents = strat_a.generate_order_intents({"A_OPT": _quote("A_OPT", 100.0), "A_HEDGE": _quote("A_HEDGE", 100.0)})
    b_intents = strat_b.generate_order_intents({"B_OPT": _quote("B_OPT", 100.0), "B_HEDGE": _quote("B_HEDGE", 100.0)})

    assert all(i.account_id == "ACC_A" for i in a_intents)
    assert all(i.account_id == "ACC_B" for i in b_intents)
    assert strat_a._vwap_count == 1
    assert strat_b._vwap_count == 1  # independent accumulators, never shared
