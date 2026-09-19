"""Strategy adapter for trading/algos/Vwap_Algo_Nifty_hedge (Phase 16.8).

Ports this algo's real VWAP-cross arm/breakdown-fire entry rule and its
stop-loss/time-exit logic (never its process/threading/broker-plumbing)
into the broker-agnostic Strategy interface, following the exact pattern
Phase 16.7/16.8 established for CombinedVwapNiftyStrategy/
DoubleStraddleStrategy. One instance represents ONE side (CE or PE) --
the legacy algo itself runs CE and PE as two fully independent
`trademanager()` instances (manager.py), never sharing state, so this
mirrors that exactly: two separate `VwapAlgoNiftyHedgeStrategy`
instances, one per side.

Ported EXACTLY from trading/algos/Vwap_Algo_Nifty_hedge/manager.py,
rest_func.py, config.py (see
docs/phase-16-8-remaining-strategy-shadow-integration-report.md Section 3
for full line-by-line provenance):
  - Hedge entry: unconditional BUY on the far-OTM hedge leg, placed once
    before the trading loop even starts -- manager.py:19.
  - Arm: `close < vwap and not in_trade and not timeup` -> istriggered,
    recording the arming candle's low/high -- manager.py:128-132.
  - Cancel-arm: `close > vwap and istriggered` -> un-arm --
    manager.py:63-67.
  - Fire (short entry): `close < triggerlow and istriggered and not
    in_trade and not timeup` -- manager.py:82. Stoploss computed exactly:
    `if triggerhigh-close>20: sl=close+20; elif 10<=triggerhigh-close<=20:
    sl=triggerhigh; else: sl=close+10` -- manager.py:84-90. Capped at
    MAX_ENTRIES (3) per instance -- manager.py:94-101/103-110.
  - Timeup: no new triggers/entries after 14:30 -- manager.py:58-60.
  - Time exit: at 15:25, buy back any open short and unconditionally exit
    the hedge -- manager.py:135-147.
  - Quantity: config.qty (195) -- config.py:86, unchanged.

DOCUMENTED SIMPLIFICATIONS (mechanism/scope, never a rule change):
  1. True VWAP (pandas_ta.vwap over a real 1-minute OHLCV candle series,
     rest_func.py:106) is NOT reproduced -- the normalized single-quote
     market-data model (Phase 16.6) carries a last-traded price, not an
     intraday volume series. A running simple average of observed prices
     is used as a documented VWAP PROXY -- the exact same proxy pattern,
     for the exact same reason, Phase 16.7 already used for
     CombinedVwapNifty's CV. The arm/cancel/fire RULES (close vs vwap,
     close vs triggerlow) are exact; only the vwap number's derivation is
     a stated approximation.
  2. `triggerlow`/`triggerhigh` are approximated as the single observed
     price at arm time (no real candle high/low is available from a
     single quote) -- both set equal to that one price. The stop-loss
     FORMULA itself (Section above) is unchanged and still produces a
     meaningful value whenever price has moved between arm and fire.
  3. Stop-loss detection is client-computed against the current quote
     (`close >= stoploss`) instead of the legacy's broker-side stoploss
     order tracked by a separate order-book-polling thread
     (rest_func.sltracking, manager.py:122). This is a MECHANISM
     substitution, not an invented rule -- the stoploss PRICE is computed
     by the exact same formula; only how its breach is detected differs,
     because the broker-order-polling infrastructure this integration
     deliberately does not touch is unavailable here.
  4. Hedge/strike selection (rest_func.get_hedge_strike's expiry-parsing
     and ATM-from-prior-close logic) is not re-derived dynamically --
     the option instrument and its hedge are supplied as constructor
     parameters, the same simplification already made for
     CombinedVwapNifty and DoubleStraddelAlgo.

Never imports any broker adapter or SDK, and never itself submits,
amends, or cancels an order -- this class only ever returns OrderIntent
objects; the execution engine remains the sole execution path.
"""
from __future__ import annotations

from datetime import datetime, time as dt_time
from typing import Callable

from trading.common.alerts import AlertManager
from trading.common.broker import OrderSide, OrderType
from trading.common.observability import AuditTrail, MetricsRegistry
from trading.common.order_intent import OrderIntent
from trading.common.strategy import BaseStrategy
from trading.common.trading_account import ExecutionMode

STRATEGY_ID = "Vwap_Algo_Nifty_hedge"

# Ported unchanged from trading/algos/Vwap_Algo_Nifty_hedge/manager.py/config.py.
MAX_ENTRIES = 3
TIMEUP_TIME = dt_time(14, 30)
FINAL_EXIT_TIME = dt_time(15, 25)


class VwapAlgoNiftyHedgeStrategy(BaseStrategy):
    def __init__(
        self,
        *,
        execution_mode: ExecutionMode | str = ExecutionMode.SHADOW,
        metrics_registry: MetricsRegistry | None = None,
        audit_trail: AuditTrail | None = None,
        alerts: AlertManager | None = None,
        option_instrument: str = "NIFTY_OPT",
        hedge_instrument: str = "NIFTY_OPT_HEDGE",
        exchange: str = "NFO",
        account_id: str = "",
        quantity: int = 195,  # config.qty, config.py:86
        max_entries: int = MAX_ENTRIES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            STRATEGY_ID, execution_mode=execution_mode,
            metrics_registry=metrics_registry, audit_trail=audit_trail, alerts=alerts,
        )
        self._option_instrument = option_instrument
        self._hedge_instrument = hedge_instrument
        self._exchange = exchange
        self._account_id = account_id
        self._quantity = quantity
        self._max_entries = max_entries
        self._clock = clock or datetime.now

        self._hedge_entered = False
        self._vwap_sum = 0.0
        self._vwap_count = 0
        self._istriggered = False
        self._triggerlow: float | None = None
        self._triggerhigh: float | None = None
        self._isintrade = False
        self._entry_price = 0.0
        self._stoploss = 0.0
        self._entry_count = 0
        self._timeup = False
        self._day_done = False

    def required_instruments(self) -> tuple[str, ...]:
        return (self._option_instrument, self._hedge_instrument)

    def _on_generate_order_intents(self) -> list[OrderIntent]:
        market_data = self.get_market_data()
        if not market_data:
            return []
        option_quote = market_data.get(self._option_instrument)
        hedge_quote = market_data.get(self._hedge_instrument)
        if option_quote is None or hedge_quote is None or option_quote.ltp is None or hedge_quote.ltp is None:
            return []

        intents: list[OrderIntent] = []
        if not self._hedge_entered:
            self._hedge_entered = True
            intents.append(self._intent(self._hedge_instrument, OrderSide.BUY, "HEDGE:ENTRY", "hedge entry (unconditional)"))

        if self._day_done:
            return intents

        close = option_quote.ltp
        vwap = self._update_and_get_vwap(close)
        now = self._clock().time()
        flagsamecandletrade = True

        if now >= TIMEUP_TIME:
            self._timeup = True

        # Trigger cancel (manager.py:63-67).
        if close > vwap and self._istriggered and not self._timeup:
            self._istriggered = False
            self._triggerlow = None
            self._triggerhigh = None

        # Stoploss (client-computed substitution -- see module docstring).
        if self._isintrade and close >= self._stoploss:
            intents.append(self._exit_position(close, "EXIT:SL"))
            flagsamecandletrade = False

        # Execution / fire (manager.py:82-119).
        if (
            self._triggerlow is not None and close < self._triggerlow
            and self._istriggered and not self._isintrade and not self._timeup
        ):
            self._istriggered = False
            spread = self._triggerhigh - close
            if spread > 20:
                stoploss = close + 20
            elif 10 <= spread <= 20:
                stoploss = self._triggerhigh
            else:
                stoploss = close + 10

            if self._entry_count >= self._max_entries:
                flagsamecandletrade = False
            else:
                self._entry_count += 1
                self._entry_price = close
                self._stoploss = stoploss
                self._triggerlow = None
                self._triggerhigh = None
                self._isintrade = True
                intents.append(self._intent(
                    self._option_instrument, OrderSide.SELL, f"ENTRY:{self._entry_count}",
                    f"short entry (VWAP breakdown), stoploss={stoploss}",
                ))

        # Vwap trigger / arm (manager.py:128-132).
        if close < vwap and not self._isintrade and not self._timeup and flagsamecandletrade:
            self._istriggered = True
            self._triggerlow = close
            self._triggerhigh = close

        # Time-based booking (manager.py:135-147).
        if now >= FINAL_EXIT_TIME and not self._day_done:
            if self._isintrade:
                intents.append(self._exit_position(close, "EXIT:TIME"))
            intents.append(self._intent(self._hedge_instrument, OrderSide.SELL, "HEDGE:EXIT", "hedge exit (final)"))
            self._day_done = True

        return intents

    def _update_and_get_vwap(self, close: float) -> float:
        self._vwap_sum += close
        self._vwap_count += 1
        return self._vwap_sum / self._vwap_count

    def _exit_position(self, _ltp: float, key_suffix: str) -> OrderIntent:
        self._isintrade = False
        return self._intent(
            self._option_instrument, OrderSide.BUY, f"{key_suffix}:{self._entry_count}",
            f"position exit ({key_suffix})",
        )

    def _intent(self, instrument: str, side: OrderSide, key_suffix: str, reason: str) -> OrderIntent:
        return OrderIntent(
            strategy_id=self.strategy_id, account_id=self._account_id, symbol=instrument,
            exchange=self._exchange, side=side, quantity=self._quantity, order_type=OrderType.MARKET,
            reason=f"Vwap_Algo_Nifty_hedge: {reason}",
            idempotency_key=f"{self.strategy_id}:{instrument}:{key_suffix}",
        )
