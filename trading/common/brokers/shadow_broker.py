"""
ShadowBroker -- a pure, non-networked BrokerClient implementation that
simulates realistic order execution for shadow/what-if runs of the
broker-agnostic architecture.

Safety model, deliberately different from AngelOneBroker's:

  - AngelOneBroker's safety is CONFIGURATION-based (empty credentials,
    read_only flag, TRADING_MODE) -- correct, but it is still, in
    principle, a class that CAN place a real order if configured to.
  - ShadowBroker's safety is STRUCTURAL: there is no SDK import, no
    network call, no credential handling anywhere in this file at all.
    There is no configuration that could ever make this class reach a
    real broker -- the only way to place a real order is to use a
    different class entirely. This is a stronger guarantee for anything
    that must be able to run unattended and simulate outcomes.

Determinism: place_order()'s default behavior is an instant full fill
(matching PaperBroker's simplicity), but a caller -- typically a test, or
a shadow-execution demo/comparison harness -- can precisely configure the
outcome of the NEXT place_order() call for a given symbol via
configure_next_order(): rejection, partial fill, or "stays open" are all
exercised deterministically, never randomly, so tests are reproducible.

Position/P&L accounting: a running weighted-average-price position per
symbol, with realized P&L booked on the closing portion of an opposite-
direction fill and unrealized P&L available via get_positions()'s
`last_price` (updated on every fill/quote).
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from trading.common.broker import (
    BrokerClient,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    Quote,
)
from trading.common.execution import TERMINAL_STATUSES, OrderState

_order_id_counter = itertools.count(1)


@dataclass
class _SimulatedOrder:
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    limit_price: float | None
    filled_quantity: int = 0
    avg_fill_price: float = 0.0
    status: str = "OPEN"  # OPEN -> COMPLETE | REJECTED | CANCELLED


class ShadowBroker(BrokerClient):
    # Phase 13 observability: never reaches a real broker, by construction
    # (see module docstring) -- StrategyExecutionEngine uses this to
    # classify fills as simulated rather than real.
    is_simulated = True

    def __init__(self) -> None:
        self._connected = False
        self._lock = threading.Lock()
        self._orders: dict[str, _SimulatedOrder] = {}
        self._positions: dict[str, Position] = {}
        self._last_prices: dict[str, float] = {}
        self._next_outcomes: dict[str, tuple[str, dict]] = {}
        self._idempotency_cache: dict[str, OrderResult] = {}

    def __repr__(self) -> str:
        return f"ShadowBroker(connected={self._connected}, orders={len(self._orders)})"

    # -- BrokerClient: connection lifecycle (no-ops, no network) -------------- #
    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # -- BrokerClient: market data (synthetic) -------------------------------- #
    def get_quote(self, symbol: str) -> Quote:
        price = self._last_prices.get(symbol, 100.0 + (hash(symbol) % 500))
        self._last_prices[symbol] = price
        return Quote(symbol=symbol, last_price=price, timestamp=datetime.now(timezone.utc).isoformat())

    # -- deterministic simulation control (test/demo hooks) -------------------- #
    def configure_next_order(self, symbol: str, outcome: str, **kwargs: Any) -> None:
        """Deterministically control the outcome of the NEXT place_order()
        call for `symbol`. Consumed exactly once (subsequent calls for the
        same symbol fall back to the default FILLED behavior unless
        reconfigured).

        outcome:
          "FILLED"   (default when never configured) -- instant full fill.
          "REJECTED" (kwargs: reason: str) -- no order created at all.
          "PARTIAL"  (kwargs: fill_ratio: float, 0 < ratio < 1) -- fills
                     that fraction immediately, order stays OPEN for the
                     remainder until fill_pending_order()/cancel_order().
          "OPEN"     -- order is accepted but never fills on its own.
        """
        self._next_outcomes[symbol] = (outcome.upper(), kwargs)

    def fill_pending_order(self, order_id: str, fill_quantity: int | None = None, price: float | None = None) -> None:
        """Advance a still-OPEN simulated order (e.g. one left PARTIAL or
        OPEN) by filling some/all of its remaining quantity. No-op if the
        order is unknown or already terminal."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None or order.status != "OPEN":
                return
            remaining = order.quantity - order.filled_quantity
            qty = remaining if fill_quantity is None else max(0, min(fill_quantity, remaining))
            if qty <= 0:
                return
            fill_price = price if price is not None else self.get_quote(order.symbol).last_price
            self._apply_fill(order, qty, fill_price)

    # -- BrokerClient: orders --------------------------------------------------- #
    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
    ) -> OrderResult:
        with self._lock:
            if quantity <= 0:
                return OrderResult(
                    order_id="", symbol=symbol, side=side, quantity=quantity,
                    status="REJECTED", message="quantity must be positive",
                )

            outcome, kwargs = self._next_outcomes.pop(symbol, ("FILLED", {}))

            if outcome == "REJECTED":
                reason = kwargs.get("reason", "simulated rejection")
                return OrderResult(order_id="", symbol=symbol, side=side, quantity=quantity, status="REJECTED", message=reason)

            order_id = f"SHADOW-{next(_order_id_counter)}"
            order = _SimulatedOrder(
                order_id=order_id, symbol=symbol, side=side, order_type=order_type,
                quantity=quantity, limit_price=limit_price,
            )
            self._orders[order_id] = order

            if outcome == "OPEN":
                return OrderResult(order_id=order_id, symbol=symbol, side=side, quantity=quantity, status="OPEN", message="simulated: awaiting fill")

            reference_price = limit_price if (order_type == OrderType.LIMIT and limit_price) else self.get_quote(symbol).last_price

            if outcome == "PARTIAL":
                ratio = kwargs.get("fill_ratio", 0.5)
                fill_qty = max(1, min(quantity - 1, round(quantity * ratio)))
                self._apply_fill(order, fill_qty, reference_price)
                return OrderResult(
                    order_id=order_id, symbol=symbol, side=side, quantity=quantity,
                    status="OPEN", message=f"simulated partial fill {fill_qty}/{quantity}",
                )

            # Default: FILLED.
            self._apply_fill(order, quantity, reference_price)
            return OrderResult(order_id=order_id, symbol=symbol, side=side, quantity=quantity, status="COMPLETE", message=f"simulated fill at {reference_price}")

    def simulate_intent(self, intent) -> OrderResult:
        """Intent-aware convenience entry point -- NOT part of BrokerClient.

        Unlike place_order() (whose ABC signature has no channel for it),
        this honors intent.idempotency_key: a second call with the SAME
        non-empty idempotency_key returns the cached result from the first
        call instead of creating a second simulated order/position update.
        """
        key = intent.idempotency_key
        if key:
            cached = self._idempotency_cache.get(key)
            if cached is not None:
                return cached

        result = self.place_order(intent.symbol, intent.side, intent.quantity, intent.order_type, intent.limit_price)

        if key:
            self._idempotency_cache[key] = result
        return result

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None or order.status != "OPEN":
                return False
            order.status = "CANCELLED"
            return True

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    # -- optional hook StrategyExecutionEngine duck-types for --------------------- #
    def get_order(self, order_id: str) -> OrderState:
        order = self._orders.get(order_id)
        if order is None:
            return OrderState(order_id=order_id, status="UNKNOWN", filled_quantity=0, remaining_quantity=0)
        return OrderState(
            order_id=order_id,
            status=order.status,
            filled_quantity=order.filled_quantity,
            remaining_quantity=max(order.quantity - order.filled_quantity, 0),
        )

    def get_order_book(self) -> list[OrderState]:
        """Every simulated order this instance has ever placed, normalized.
        Not part of the BrokerClient ABC -- optional, mirrors AngelOneBroker's
        own get_order_book() shape for parity."""
        return [self.get_order(order_id) for order_id in self._orders]

    def get_open_orders(self) -> list[OrderState]:
        return [state for state in self.get_order_book() if state.status not in TERMINAL_STATUSES]

    def modify_order(self, order_id: str, quantity: int, limit_price: float) -> bool:
        """Optional hook (see trading.common.execution's duck-typed
        get_order/modify_order contract). `quantity` is the new REMAINING
        (unfilled) quantity to re-price -- matching AngelOneBroker.
        modify_order()'s own semantics -- not the order's original total."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None or order.status != "OPEN":
                return False
            order.quantity = order.filled_quantity + quantity
            order.limit_price = limit_price
            return True

    # -- position/P&L accounting -------------------------------------------------- #
    def _apply_fill(self, order: _SimulatedOrder, fill_qty: int, fill_price: float) -> None:
        previously_filled = order.filled_quantity
        order.filled_quantity += fill_qty
        order.avg_fill_price = (
            fill_price
            if previously_filled == 0
            else ((order.avg_fill_price * previously_filled) + fill_price * fill_qty) / order.filled_quantity
        )
        if order.filled_quantity >= order.quantity:
            order.status = "COMPLETE"
        self._update_position(order.symbol, order.side, fill_qty, fill_price)

    def _update_position(self, symbol: str, side: OrderSide, fill_qty: int, fill_price: float) -> None:
        signed_qty = fill_qty if side == OrderSide.BUY else -fill_qty
        existing = self._positions.get(symbol)

        if existing is None or existing.quantity == 0:
            realized = existing.pnl if existing else 0.0
            self._positions[symbol] = Position(
                symbol=symbol, quantity=signed_qty, average_price=fill_price, last_price=fill_price, pnl=round(realized, 2),
            )
            return

        same_direction = (existing.quantity > 0) == (signed_qty > 0)
        new_qty = existing.quantity + signed_qty

        if same_direction:
            total_qty = abs(existing.quantity) + abs(signed_qty)
            avg_price = ((existing.average_price * abs(existing.quantity)) + (fill_price * abs(signed_qty))) / total_qty
            realized = existing.pnl
        else:
            closing_qty = min(abs(signed_qty), abs(existing.quantity))
            direction = 1 if existing.quantity > 0 else -1
            realized = existing.pnl + direction * (fill_price - existing.average_price) * closing_qty
            if new_qty == 0:
                avg_price = 0.0
            elif (new_qty > 0) == (existing.quantity > 0):
                avg_price = existing.average_price  # partial close, same direction remains
            else:
                avg_price = fill_price  # flipped direction: closed fully + opened the other way

        self._positions[symbol] = Position(
            symbol=symbol, quantity=new_qty, average_price=avg_price, last_price=fill_price, pnl=round(realized, 2),
        )
