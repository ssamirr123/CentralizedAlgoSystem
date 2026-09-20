"""Strategy adapter for trading/algos/DoubleStraddelAlgo (Phase 16.8).

Ports this algo's real, wall-clock-scheduled hedged-short-straddle
decision rules (never its process/threading/broker-plumbing) into the
broker-agnostic Strategy interface, following the exact pattern Phase
16.7 established for CombinedVwapNiftyStrategy.

Ported EXACTLY from trading/algos/DoubleStraddelAlgo/config.py,
strategy/engine.py, strategy/straddle.py, strategy/hedge.py,
risk/guard.py (see docs/phase-16-8-remaining-strategy-shadow-integration-report.md
Section 3 for full line-by-line provenance):
  - Hedge entry: unconditional BUY on both hedge legs at HEDGE_ENTRY
    (10:20 IST) -- config.py:95.
  - Straddle entry: unconditional SELL on both ATM legs at MORNING_ENTRY
    (10:25) and AFT_ENTRY (14:16), each gated on the hedge having already
    been entered (engine.py:56,71) -- no price condition.
  - Per-leg exit: SL = entry + SL_POINTS (25); target = entry -
    TARGET_POINTS (50) -- straddle.py:43-44,51,54 (short options: a
    higher price is a loss, a lower price is the target).
  - Time exits: MORNING_EXIT (14:14, morning session only) and FINAL_EXIT
    (15:25, afternoon session + both hedge legs) -- engine.py:63-65,78-82.
  - Portfolio kill switch: combined realized+unrealized MTM <=
    -DAILY_MAX_LOSS (20000) -> close everything, stop the day --
    risk/guard.py:52.
  - Quantity: LOT_QTY (65) -- config.py:92, unchanged for every leg.

DOCUMENTED SIMPLIFICATIONS (mechanism/scope, never a rule change):
  1. Hedge/ATM strike SELECTION (spot-LTP-based ATM rounding +
     expiry-day-dependent hedge gap, expiry.py:36-99) is NOT re-derived
     dynamically here -- the four option instruments (hedge CE/PE,
     straddle CE/PE) are supplied as constructor parameters, exactly the
     same simplification Phase 16.7 made for CombinedVwapNifty's
     ce_instrument/pe_instrument (strike-locking deferred, not invented).
  2. Wall-clock comparisons use an injectable `clock` callable (default
     `datetime.now`, naive local time) rather than the legacy's IST-aware
     scheduling -- for real deployment a caller must inject a proper
     IST-aware clock; this integration does not invent timezone handling
     that isn't already present in the normalized market-data model.
  3. The afternoon straddle re-uses the SAME ATM instruments as the
     morning straddle (no intraday re-locking) -- consistent with
     simplification 1.

Never imports any broker adapter or SDK, and never itself submits,
amends, or cancels an order -- this class only ever returns OrderIntent
objects; the execution engine remains the sole execution path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from typing import Callable

from trading.common.alerts import AlertManager
from trading.common.broker import OrderSide, OrderType
from trading.common.observability import AuditTrail, MetricsRegistry
from trading.common.order_intent import OrderIntent
from trading.common.strategy import BaseStrategy
from trading.common.trading_account import ExecutionMode

STRATEGY_ID = "DoubleStraddelAlgo"

# Ported unchanged from trading/algos/DoubleStraddelAlgo/config.py.
SL_POINTS = 25
TARGET_POINTS = 50
DAILY_MAX_LOSS = 20000.0
HEDGE_ENTRY_TIME = dt_time(10, 20)
MORNING_ENTRY_TIME = dt_time(10, 25)
MORNING_EXIT_TIME = dt_time(14, 14)
AFTERNOON_ENTRY_TIME = dt_time(14, 16)
FINAL_EXIT_TIME = dt_time(15, 25)


@dataclass
class _LegState:
    in_position: bool = False
    entry_price: float = 0.0
    realized_pnl: float = 0.0  # short leg: positive = profit


@dataclass
class _SessionState:
    entered: bool = False
    exited: bool = False
    ce: _LegState = field(default_factory=_LegState)
    pe: _LegState = field(default_factory=_LegState)


class DoubleStraddleStrategy(BaseStrategy):
    def __init__(
        self,
        *,
        execution_mode: ExecutionMode | str = ExecutionMode.SHADOW,
        metrics_registry: MetricsRegistry | None = None,
        audit_trail: AuditTrail | None = None,
        alerts: AlertManager | None = None,
        hedge_ce_instrument: str = "NIFTY_HEDGE_CE",
        hedge_pe_instrument: str = "NIFTY_HEDGE_PE",
        straddle_ce_instrument: str = "NIFTY_ATM_CE",
        straddle_pe_instrument: str = "NIFTY_ATM_PE",
        exchange: str = "NFO",
        account_id: str = "",
        quantity: int = 65,  # LOT_QTY, config.py:92
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            STRATEGY_ID, execution_mode=execution_mode,
            metrics_registry=metrics_registry, audit_trail=audit_trail, alerts=alerts,
        )
        self._hedge_ce = hedge_ce_instrument
        self._hedge_pe = hedge_pe_instrument
        self._straddle_ce = straddle_ce_instrument
        self._straddle_pe = straddle_pe_instrument
        self._exchange = exchange
        self._account_id = account_id
        self._quantity = quantity
        self._clock = clock or datetime.now

        self._hedge_active = False
        self._hedge_ce_leg = _LegState()
        self._hedge_pe_leg = _LegState()
        self._morning = _SessionState()
        self._afternoon = _SessionState()
        self._day_stopped = False

    def required_instruments(self) -> tuple[str, ...]:
        return (self._hedge_ce, self._hedge_pe, self._straddle_ce, self._straddle_pe)

    # -- main evaluation -------------------------------------------------------- #
    def _on_generate_order_intents(self) -> list[OrderIntent]:
        market_data = self.get_market_data()
        if not market_data:
            return []
        prices = {}
        for instrument in self.required_instruments():
            quote = market_data.get(instrument)
            if quote is None or quote.ltp is None:
                return []
            prices[instrument] = quote.ltp

        if self._day_stopped:
            return []

        now = self._clock().time()
        intents: list[OrderIntent] = []

        kill_switch_intents = self._check_kill_switch(prices)
        if kill_switch_intents:
            return kill_switch_intents  # emergency square-off supersedes everything else this cycle

        if now >= HEDGE_ENTRY_TIME and not self._hedge_active:
            intents.extend(self._enter_hedge(prices))

        if self._hedge_active:
            intents.extend(self._run_session(
                self._morning, "morning", MORNING_ENTRY_TIME, MORNING_EXIT_TIME, now, prices,
            ))
            intents.extend(self._run_session(
                self._afternoon, "afternoon", AFTERNOON_ENTRY_TIME, None, now, prices,
            ))

        if now >= FINAL_EXIT_TIME and not self._day_stopped:
            intents.extend(self._final_exit(prices))
            self._day_stopped = True

        return intents

    # -- hedge -------------------------------------------------------------- #
    def _enter_hedge(self, prices: dict[str, float]) -> list[OrderIntent]:
        self._hedge_active = True
        self._hedge_ce_leg.in_position = True
        self._hedge_ce_leg.entry_price = prices[self._hedge_ce]
        self._hedge_pe_leg.in_position = True
        self._hedge_pe_leg.entry_price = prices[self._hedge_pe]
        return [
            self._intent(self._hedge_ce, OrderSide.BUY, "HEDGE:ENTRY", "hedge entry (unconditional, 10:20)"),
            self._intent(self._hedge_pe, OrderSide.BUY, "HEDGE:ENTRY", "hedge entry (unconditional, 10:20)"),
        ]

    # -- straddle session (morning/afternoon) -------------------------------- #
    def _run_session(
        self, session: _SessionState, name: str, entry_time: dt_time, exit_time: dt_time | None,
        now: dt_time, prices: dict[str, float],
    ) -> list[OrderIntent]:
        intents: list[OrderIntent] = []

        if not session.entered and now >= entry_time:
            session.entered = True
            session.ce.in_position = True
            session.ce.entry_price = prices[self._straddle_ce]
            session.pe.in_position = True
            session.pe.entry_price = prices[self._straddle_pe]
            intents.append(self._intent(self._straddle_ce, OrderSide.SELL, f"{name}:ENTRY", f"{name} straddle entry"))
            intents.append(self._intent(self._straddle_pe, OrderSide.SELL, f"{name}:ENTRY", f"{name} straddle entry"))
            return intents  # entry and SL/target check never share the same cycle (mirrors straddle.py's own separate monitor loop)

        if session.entered and not session.exited:
            for leg, instrument in ((session.ce, self._straddle_ce), (session.pe, self._straddle_pe)):
                if not leg.in_position:
                    continue
                ltp = prices[instrument]
                sl = leg.entry_price + SL_POINTS
                target = leg.entry_price - TARGET_POINTS
                if ltp >= sl:
                    intents.append(self._close_leg(leg, instrument, ltp, f"{name}:SL"))
                elif ltp <= target:
                    intents.append(self._close_leg(leg, instrument, ltp, f"{name}:TARGET"))

            if exit_time is not None and now >= exit_time:
                for leg, instrument in ((session.ce, self._straddle_ce), (session.pe, self._straddle_pe)):
                    if leg.in_position:
                        intents.append(self._close_leg(leg, instrument, prices[instrument], f"{name}:TIME_EXIT"))
                session.exited = True

        return intents

    def _close_leg(self, leg: _LegState, instrument: str, ltp: float, reason: str) -> OrderIntent:
        leg.realized_pnl += leg.entry_price - ltp  # short: profit when price fell
        leg.in_position = False
        return self._intent(instrument, OrderSide.BUY, f"{reason}", f"straddle leg exit: {reason}")

    # -- portfolio kill switch ------------------------------------------------ #
    def _check_kill_switch(self, prices: dict[str, float]) -> list[OrderIntent]:
        mtm = 0.0
        for leg, instrument, is_hedge in (
            (self._hedge_ce_leg, self._hedge_ce, True), (self._hedge_pe_leg, self._hedge_pe, True),
            (self._morning.ce, self._straddle_ce, False), (self._morning.pe, self._straddle_pe, False),
            (self._afternoon.ce, self._straddle_ce, False), (self._afternoon.pe, self._straddle_pe, False),
        ):
            mtm += leg.realized_pnl
            if leg.in_position:
                # hedge legs are long (profit when price rises); straddle legs are short.
                unrealized = (prices[instrument] - leg.entry_price) if is_hedge else (leg.entry_price - prices[instrument])
                mtm += unrealized

        if mtm > -abs(DAILY_MAX_LOSS):
            return []

        self._day_stopped = True
        return self._final_exit(prices)

    def _final_exit(self, prices: dict[str, float]) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        for session, name in ((self._morning, "morning"), (self._afternoon, "afternoon")):
            for leg, instrument in ((session.ce, self._straddle_ce), (session.pe, self._straddle_pe)):
                if leg.in_position:
                    intents.append(self._close_leg(leg, instrument, prices[instrument], f"{name}:FINAL_EXIT"))
        for leg, instrument, side in (
            (self._hedge_ce_leg, self._hedge_ce, OrderSide.SELL), (self._hedge_pe_leg, self._hedge_pe, OrderSide.SELL),
        ):
            if leg.in_position:
                leg.realized_pnl += prices[instrument] - leg.entry_price  # hedge is long
                leg.in_position = False
                intents.append(self._intent(instrument, side, "HEDGE:EXIT", "hedge exit (final/kill-switch)"))
        return intents

    def _intent(self, instrument: str, side: OrderSide, key_suffix: str, reason: str) -> OrderIntent:
        # Phase 17.1-R Remediation C: the idempotency key previously carried
        # no trading-date component at all (strategy_id:instrument:suffix,
        # where suffix is a fixed time-slot label like "morning:ENTRY") --
        # the exact same key was regenerated on every trading day, so a real
        # day-2 order would have been treated as a replay of day-1's
        # COMPLETED order and never reached the broker. Deriving the date
        # from self._clock() (the same injectable clock already used for
        # every wall-clock comparison above, see __init__'s own docstring)
        # uses this strategy's existing "canonical trading timezone" seam
        # rather than inventing a new one: same trading day -> same date
        # string regardless of worker restart; a new trading day -> a
        # different key, exactly as a genuinely new logical event requires.
        # No entry/exit/SL/target/timing rule above this line changed.
        trading_date = self._clock().date().isoformat()
        return OrderIntent(
            strategy_id=self.strategy_id, account_id=self._account_id, symbol=instrument,
            exchange=self._exchange, side=side, quantity=self._quantity, order_type=OrderType.MARKET,
            reason=f"DoubleStraddelAlgo: {reason}",
            idempotency_key=f"{self.strategy_id}:{trading_date}:{instrument}:{key_suffix}",
        )
