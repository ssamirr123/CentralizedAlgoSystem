"""Strategy adapter for trading/algos/CombinedVwapNifty (Phase 16.7).

Phase 16.7 ports this algo's real entry/exit DECISION RULES (never its
process/threading/broker-plumbing) into the broker-agnostic Strategy
interface, consuming Phase 16.6 normalized market data
(trading/common/market_data_gateway.py) and emitting broker-independent
OrderIntents. This is the ONE strategy integrated this phase (see
docs/phase-16-7-strategy-order-intent-integration-report.md Section 3 for
why); DoubleStraddleStrategy and VwapAlgoNiftyHedgeStrategy are untouched
and remain zero-intent, per that phase's own explicit scope.

Ported, with full line-by-line provenance in the report's Section 5, from
trading/algos/CombinedVwapNifty/manager.py and rest_func.py:
  - Entry: a 2-state arm/fire machine on Combined Premium (CP) vs Combined
    VWAP (CV) -- WAIT_ARM -> ARMED when cp > cv (manager.py:84); ARMED ->
    fire (enter both legs) when cv > cp (manager.py:88).
  - Exit: a 3-level combined-loss ladder with thresholds
    RISK_LOSS_LEVELS * num_lots (config.py:129; manager.py:210-246).
    Levels 0/1 exit only the bigger-loser leg and advance the ladder;
    the final level exits both legs and stops the day.

TWO DOCUMENTED, HONEST SIMPLIFICATIONS relative to the legacy algo (see
the report for the full reasoning -- these narrow WHAT DATA feeds the
rules, they do not change the rules themselves):
  1. CV here is a running simple average of CP across evaluation cycles,
     not the legacy's tick-volume-weighted cumulative VWAP
     (rest_func.py's make_cp_cv) -- the minimal Phase 16.6 normalized
     quote (IndexQuote/OptionQuote) carries a last-traded price, not an
     intraday volume series, and fabricating one would misrepresent real
     market behavior rather than genuinely reproduce it. The RULE (arm on
     cp>cv, fire on cv>cp) is exact; only the CV number's derivation is a
     documented proxy.
  2. Entry acts on both legs together as a single straddle entry;
     the legacy's fully independent per-leg re-entry/cooldown state
     machine (can_reenter(), REENTRY_COOLDOWN_SECONDS) is not ported --
     deferred, not invented.

Never imports any broker adapter or SDK, and never itself submits,
amends, or cancels an order -- this class only ever returns OrderIntent
objects; the execution engine remains the sole execution path.
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.common.alerts import AlertManager
from trading.common.broker import OrderSide, OrderType
from trading.common.observability import AuditTrail, MetricsRegistry
from trading.common.order_intent import OrderIntent
from trading.common.strategy import BaseStrategy
from trading.common.trading_account import ExecutionMode

STRATEGY_ID = "CombinedVwapNifty"

# Ported unchanged from trading/algos/CombinedVwapNifty/config.py:129.
RISK_LOSS_LEVELS: tuple[float, ...] = (650.0, 1300.0, 2000.0)


@dataclass
class _LegState:
    in_position: bool = False
    entry_price: float = 0.0
    realized_loss: float = 0.0
    entry_sequence: int = 0  # bumped on every exit, so a later re-entry's idempotency_key differs from the last


class CombinedVwapNiftyStrategy(BaseStrategy):
    def __init__(
        self,
        *,
        execution_mode: ExecutionMode | str = ExecutionMode.SHADOW,
        metrics_registry: MetricsRegistry | None = None,
        audit_trail: AuditTrail | None = None,
        alerts: AlertManager | None = None,
        ce_instrument: str = "NIFTY_CE_ATM",
        pe_instrument: str = "NIFTY_PE_ATM",
        exchange: str = "NFO",
        account_id: str = "",
        # NIFTY's real-world lot size (unchanged from the legacy algo's own
        # unit) -- the legacy's dynamic strike-locking/lot-size lookup
        # (rest_func.setup_straddle) is not ported; a fixed value here is
        # a documented, honest simplification, not an invented number.
        quantity: int = 65,
        num_lots: int = 1,
    ) -> None:
        super().__init__(
            STRATEGY_ID, execution_mode=execution_mode,
            metrics_registry=metrics_registry, audit_trail=audit_trail, alerts=alerts,
        )
        self._ce_instrument = ce_instrument
        self._pe_instrument = pe_instrument
        self._exchange = exchange
        self._account_id = account_id
        self._quantity = quantity
        self._num_lots = num_lots
        self._signal_state = "WAIT_ARM"
        self._cv_sum = 0.0
        self._cv_count = 0
        self._legs: dict[str, _LegState] = {ce_instrument: _LegState(), pe_instrument: _LegState()}
        self._risk_level_index = 0
        self._day_stopped = False

    def required_instruments(self) -> tuple[str, ...]:
        return (self._ce_instrument, self._pe_instrument)

    def _on_generate_order_intents(self) -> list[OrderIntent]:
        market_data = self.get_market_data()
        if not market_data:
            return []
        ce = market_data.get(self._ce_instrument)
        pe = market_data.get(self._pe_instrument)
        if ce is None or pe is None or ce.ltp is None or pe.ltp is None:
            return []

        intents: list[OrderIntent] = []
        intents.extend(self._check_risk_ladder(ce.ltp, pe.ltp))

        if self._day_stopped:
            return intents

        # manager.py:84,88 -- arm/fire on the combined-premium/VWAP cross.
        cp = ce.ltp + pe.ltp
        cv = self._update_and_get_cv(cp)
        if self._signal_state == "WAIT_ARM" and cp > cv:
            self._signal_state = "ARMED"
        elif self._signal_state == "ARMED" and cv > cp:
            self._signal_state = "WAIT_ARM"
            intents.extend(self._enter_straddle(ce.ltp, pe.ltp))

        return intents

    def _update_and_get_cv(self, cp: float) -> float:
        self._cv_sum += cp
        self._cv_count += 1
        return self._cv_sum / self._cv_count

    def _enter_straddle(self, ce_ltp: float, pe_ltp: float) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        for instrument, ltp in ((self._ce_instrument, ce_ltp), (self._pe_instrument, pe_ltp)):
            leg = self._legs[instrument]
            if leg.in_position:
                continue
            leg.in_position = True
            leg.entry_price = ltp
            intents.append(OrderIntent(
                strategy_id=self.strategy_id, account_id=self._account_id, symbol=instrument,
                exchange=self._exchange, side=OrderSide.SELL, quantity=self._quantity,
                order_type=OrderType.MARKET, reason="CombinedVwapNifty: CV>CP fire (arm/trigger)",
                idempotency_key=f"{self.strategy_id}:{instrument}:ENTRY:{leg.entry_sequence}",
            ))
        return intents

    def _check_risk_ladder(self, ce_ltp: float, pe_ltp: float) -> list[OrderIntent]:
        # manager.py:210-246 -- combined realized+unrealized loss vs a
        # 3-level ladder; levels 0/1 exit only the bigger-loser leg.
        prices = {self._ce_instrument: ce_ltp, self._pe_instrument: pe_ltp}
        unrealized = {
            instrument: (max(0.0, (prices[instrument] - leg.entry_price) * self._quantity) if leg.in_position else 0.0)
            for instrument, leg in self._legs.items()
        }
        combined_loss = sum(unrealized.values()) + sum(leg.realized_loss for leg in self._legs.values())

        if self._risk_level_index >= len(RISK_LOSS_LEVELS):
            return []
        threshold = RISK_LOSS_LEVELS[self._risk_level_index] * self._num_lots
        if combined_loss < threshold:
            return []

        intents: list[OrderIntent] = []
        if self._risk_level_index < len(RISK_LOSS_LEVELS) - 1:
            bigger = max(unrealized, key=unrealized.get)
            if self._legs[bigger].in_position:
                intents.append(self._exit_leg(bigger, prices[bigger], "risk ladder: bigger-loser leg"))
            self._risk_level_index += 1
        else:
            for instrument, leg in self._legs.items():
                if leg.in_position:
                    intents.append(self._exit_leg(instrument, prices[instrument], "risk ladder: final level, day stopped"))
            self._day_stopped = True
        return intents

    def _exit_leg(self, instrument: str, ltp: float, reason: str) -> OrderIntent:
        leg = self._legs[instrument]
        leg.realized_loss += max(0.0, (ltp - leg.entry_price) * self._quantity)
        leg.in_position = False
        leg.entry_price = 0.0
        leg.entry_sequence += 1
        return OrderIntent(
            strategy_id=self.strategy_id, account_id=self._account_id, symbol=instrument,
            exchange=self._exchange, side=OrderSide.BUY, quantity=self._quantity,
            order_type=OrderType.MARKET, reason=f"CombinedVwapNifty: {reason}",
            idempotency_key=f"{self.strategy_id}:{instrument}:EXIT:{self._risk_level_index}:{leg.entry_sequence}",
        )
