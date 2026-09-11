"""
Broker-agnostic strategy execution engine.

trading.common.broker.BrokerClient gives every adapter (paper, Zerodha,
AngelOne, ICICI Breeze) the same primitives: connect, get_quote,
place_order, cancel_order, get_positions. That's enough for a fire-and-
forget market order, but every strategy that actually manages LIMIT orders
(trading/algos/DoubleStraddelAlgo/broker/orders.py is the reference
example) has reimplemented the same order-lifecycle logic directly against
its broker's raw SDK: retry-with-reprice, pending-order timeout handling,
partial-fill detection, and throttled/coalesced reads. This module lifts
that logic out and implements it once, against BrokerClient only, so it
works unchanged for whichever broker a strategy is configured with.

Pending-order lifecycle management (re-pricing, timeout handling) is an
OPTIONAL capability an adapter opts into by implementing two extra methods
that BrokerClient does not declare as abstract:

    get_order(order_id: str) -> OrderState
    modify_order(order_id: str, quantity: int, limit_price: float) -> bool

They're duck-typed rather than added to the BrokerClient ABC so that
adapters which fill synchronously (PaperBroker) or haven't implemented them
yet (the still-stubbed Zerodha/AngelOne/ICICI adapters) are unaffected:
StrategyExecutionEngine simply trusts place_order()'s returned status as
final when an adapter has nothing more to report.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from trading.common.broker import (
    BrokerClient,
    LiveTradingDisabledError,
    OrderResult,
    OrderSide,
    OrderType,
)

# Statuses that mean "nothing left to manage" across brokers. Adapters may
# use their own vocabulary beyond this (e.g. "TRIGGER PENDING" is NOT
# terminal) — anything not in this set is treated as still-open.
TERMINAL_STATUSES = {"FILLED", "COMPLETE", "REJECTED", "CANCELLED"}


@dataclass(frozen=True)
class OrderState:
    """Point-in-time status of a previously placed order. An adapter that
    supports pending-order management returns this from get_order()."""

    order_id: str
    status: str
    filled_quantity: int
    remaining_quantity: int


@dataclass
class ExecutionConfig:
    """Tunables for StrategyExecutionEngine. Defaults are broker-neutral —
    a strategy overrides them per its own risk/latency profile."""

    max_retries: int = 3
    retry_delay_seconds: float = 1.0
    # Price bump applied per retry attempt, in price units, walking the
    # limit toward the market so a repeatedly-unfilled order gets more
    # fillable each attempt.
    limit_slippage: float = 0.05
    tick_size: float = 0.05
    pending_timeout_seconds: float = 10.0
    # What to do with a LIMIT order still open after pending_timeout_seconds:
    # "CANCEL" | "MODIFY" (re-price the remainder) | "MARKET" (requires
    # allow_market_emergency=True).
    pending_action: str = "CANCEL"
    allow_market_emergency: bool = False
    # Minimum gap enforced between any two broker API calls made by this
    # engine, so concurrent legs of a multi-leg strategy don't trip a
    # broker's rate limit by hammering it in the same instant.
    min_api_interval_seconds: float = 0.35
    dry_run: bool = False


class StrategyExecutionEngine:
    """Places and manages LIMIT orders against any BrokerClient.

    One engine instance per strategy/broker session — the rate-limit
    throttle and pending-order registry are per-instance state, not global.
    """

    def __init__(
        self,
        broker: BrokerClient,
        config: ExecutionConfig | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._broker = broker
        self._config = config or ExecutionConfig()
        self._log = on_log or (lambda msg: None)
        self._api_lock = threading.Lock()
        self._last_api_ts = 0.0
        self._pending_lock = threading.Lock()
        self._pending_orders: dict[str, OrderResult] = {}

    # -- rate limiting --------------------------------------------------- #
    def _throttle(self) -> None:
        with self._api_lock:
            wait = self._config.min_api_interval_seconds - (time.monotonic() - self._last_api_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_api_ts = time.monotonic()

    def _retry(self, fn: Callable[[int], object], label: str):
        cfg = self._config
        for attempt in range(1, cfg.max_retries + 1):
            try:
                self._throttle()
                result = fn(attempt)
                if result:
                    return result
            except LiveTradingDisabledError:
                raise
            except Exception as exc:
                self._log(f"[EXEC RETRY] {label} attempt {attempt}/{cfg.max_retries} error: {exc}")
            time.sleep(cfg.retry_delay_seconds)
        self._log(f"[EXEC FAILED] {label} - {cfg.max_retries} retries exhausted")
        return None

    def _limit_price(self, symbol: str, side: OrderSide, attempt: int) -> float:
        quote = self._broker.get_quote(symbol)
        bump = self._config.limit_slippage * attempt
        raw = quote.last_price + bump if side == OrderSide.BUY else max(self._config.tick_size, quote.last_price - bump)
        return round(raw / self._config.tick_size) * self._config.tick_size

    # -- public API -------------------------------------------------------- #
    def place_limit(self, symbol: str, side: OrderSide, quantity: int) -> OrderResult | None:
        """Place a LIMIT order with retry+reprice; manage it in the background if it stays open."""
        if self._config.dry_run:
            price = self._limit_price(symbol, side, 1)
            result = OrderResult(
                order_id=f"DRYRUN-{int(time.time() * 1000)}",
                symbol=symbol,
                side=side,
                quantity=quantity,
                status="FILLED",
                message=f"dry-run at {price}",
            )
            self._log(f"[DRY RUN] LIMIT {side.value} {symbol} qty={quantity} price={price} id={result.order_id}")
            return result

        def _call(attempt: int) -> OrderResult:
            price = self._limit_price(symbol, side, attempt)
            result = self._broker.place_order(symbol, side, quantity, OrderType.LIMIT, price)
            if result.status == "REJECTED":
                raise RuntimeError(result.message or "order rejected")
            self._log(
                f"[EXEC] LIMIT {side.value} {symbol} qty={quantity} price={price} "
                f"id={result.order_id} status={result.status}"
            )
            return result

        result = self._retry(_call, f"place_limit({symbol},{side.value})")
        if result and result.status not in TERMINAL_STATUSES:
            with self._pending_lock:
                self._pending_orders[result.order_id] = result
            # Manage the pending order on a background thread so placement
            # returns immediately — keeps multiple strategy legs effectively
            # simultaneous instead of serialized behind each other's timeout.
            threading.Thread(target=self._manage_pending, args=(result,), daemon=True).start()
        return result

    def place_market_emergency(self, symbol: str, side: OrderSide, quantity: int) -> OrderResult | None:
        """MARKET order for the emergency square-off path only. Falls back to
        place_limit() unless the strategy has explicitly enabled market orders."""
        if not self._config.allow_market_emergency:
            return self.place_limit(symbol, side, quantity)

        if self._config.dry_run:
            result = OrderResult(
                order_id=f"DRYRUN-{int(time.time() * 1000)}",
                symbol=symbol,
                side=side,
                quantity=quantity,
                status="FILLED",
                message="dry-run market(emergency)",
            )
            self._log(f"[DRY RUN] MARKET(EMERGENCY) {side.value} {symbol} qty={quantity} id={result.order_id}")
            return result

        def _call(_attempt: int) -> OrderResult:
            result = self._broker.place_order(symbol, side, quantity, OrderType.MARKET)
            if result.status == "REJECTED":
                raise RuntimeError(result.message or "order rejected")
            self._log(f"[EXEC] MARKET(EMERGENCY) {side.value} {symbol} qty={quantity} id={result.order_id} status={result.status}")
            return result

        return self._retry(_call, f"place_market_emergency({symbol},{side.value})")

    def cancel(self, order_id: str) -> bool:
        if self._config.dry_run:
            self._log(f"[DRY RUN] CANCEL id={order_id}")
            return True

        def _call(_attempt: int) -> bool:
            return bool(self._broker.cancel_order(order_id))

        return bool(self._retry(_call, f"cancel({order_id})"))

    def cancel_all_pending(self) -> None:
        """Cancel every order this engine instance is still tracking as open."""
        with self._pending_lock:
            order_ids = list(self._pending_orders.keys())
        for order_id in order_ids:
            self.cancel(order_id)

    # -- pending-order management ------------------------------------------ #
    def _manage_pending(self, placed: OrderResult) -> None:
        time.sleep(self._config.pending_timeout_seconds)
        get_order = getattr(self._broker, "get_order", None)
        if get_order is None:
            # Adapter can't report pending status — nothing more we can do.
            self._pop_pending(placed.order_id)
            return

        self._throttle()
        try:
            state = get_order(placed.order_id)
        except NotImplementedError:
            self._pop_pending(placed.order_id)
            return

        if state.status in TERMINAL_STATUSES:
            self._pop_pending(placed.order_id)
            return

        remainder = state.remaining_quantity
        if state.filled_quantity and remainder:
            self._log(
                f"[PARTIAL FILL] {placed.symbol} filled={state.filled_quantity}/{placed.quantity} "
                "- managing remainder only"
            )
        if remainder <= 0:
            self._pop_pending(placed.order_id)
            return

        action = self._config.pending_action
        if action == "CANCEL":
            self.cancel(placed.order_id)
        elif action == "MODIFY":
            modify_order = getattr(self._broker, "modify_order", None)
            if modify_order is not None:
                price = self._limit_price(placed.symbol, placed.side, 2)
                self._throttle()
                modify_order(placed.order_id, remainder, price)
                self._log(f"[EXEC] MODIFY {placed.symbol} id={placed.order_id} qty={remainder} price={price}")
        elif action == "MARKET" and self._config.allow_market_emergency:
            self.cancel(placed.order_id)
            self.place_market_emergency(placed.symbol, placed.side, remainder)

        self._pop_pending(placed.order_id)

    def _pop_pending(self, order_id: str) -> None:
        with self._pending_lock:
            self._pending_orders.pop(order_id, None)
