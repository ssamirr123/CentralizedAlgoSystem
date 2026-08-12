"""
Paper (simulated) broker. No network calls, no real credentials, no live
orders — ever. This is the default broker (BROKER=paper) so a fresh
checkout with no configuration cannot accidentally touch a real account.

Fills are instant and deterministic (fills at the requested/last price).
This is for exercising the strategy/runtime plumbing, not for realistic
backtesting — it deliberately does not simulate slippage, partial fills,
or rejections beyond basic validation.
"""
from __future__ import annotations

import itertools
import random
from datetime import datetime, timezone

from trading.common.broker import (
    BrokerClient,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    Quote,
)

_order_id_counter = itertools.count(1)


class PaperBroker(BrokerClient):
    def __init__(self) -> None:
        self._connected = False
        self._positions: dict[str, Position] = {}
        self._last_prices: dict[str, float] = {}

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_quote(self, symbol: str) -> Quote:
        # Deterministic-ish synthetic price so repeated calls aren't wildly random.
        base = self._last_prices.get(symbol, 100.0 + (hash(symbol) % 500))
        price = round(base * random.uniform(0.999, 1.001), 2)
        self._last_prices[symbol] = price
        return Quote(symbol=symbol, last_price=price, timestamp=datetime.now(timezone.utc).isoformat())

    def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
    ) -> OrderResult:
        if quantity <= 0:
            return OrderResult(
                order_id="",
                symbol=symbol,
                side=side,
                quantity=quantity,
                status="REJECTED",
                message="quantity must be positive",
            )

        fill_price = limit_price if order_type == OrderType.LIMIT and limit_price else self.get_quote(symbol).last_price
        order_id = f"PAPER-{next(_order_id_counter)}"

        existing = self._positions.get(symbol)
        signed_qty = quantity if side == OrderSide.BUY else -quantity
        if existing is None:
            self._positions[symbol] = Position(
                symbol=symbol,
                quantity=signed_qty,
                average_price=fill_price,
                last_price=fill_price,
                pnl=0.0,
            )
        else:
            new_qty = existing.quantity + signed_qty
            self._positions[symbol] = Position(
                symbol=symbol,
                quantity=new_qty,
                average_price=fill_price if new_qty != 0 else existing.average_price,
                last_price=fill_price,
                pnl=existing.pnl,
            )

        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            status="FILLED",
            message=f"paper fill at {fill_price}",
        )

    def cancel_order(self, order_id: str) -> bool:
        # Paper fills are instant, so there's nothing pending to cancel.
        return False

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())
