"""
AccountState -- a normalized, broker-independent snapshot of one account's
funds/positions/orders (Phase 15B section 13).

Angel One, Dhan and ICICI Breeze each return their own response shapes
(FundsSnapshot, adapter-specific order-book rows, ...). This module does not
change any adapter's return types -- it only defines the common shape a
caller can fold any of them into, plus a small builder that accepts already-
normalized trading.common.broker.Position objects (every adapter already
returns those) and generic dict-shaped funds/orders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from trading.common.broker import Position


@dataclass(frozen=True)
class AccountState:
    account_id: str
    broker_id: str
    available_cash: float
    used_margin: float
    positions: tuple[Position, ...] = ()
    open_orders: tuple[dict[str, Any], ...] = ()
    trades: tuple[dict[str, Any], ...] = ()
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def position_count(self) -> int:
        return len(self.positions)

    @property
    def open_order_count(self) -> int:
        return len(self.open_orders)


def build_account_state(
    *,
    account_id: str,
    broker_id: str,
    available_cash: float,
    used_margin: float,
    positions: list[Position] | tuple[Position, ...] = (),
    open_orders: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    trades: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> AccountState:
    """The one place that assembles an AccountState, so every caller (the
    account router, a future dashboard endpoint, a validation script) builds
    the same shape the same way instead of hand-rolling the dataclass."""
    return AccountState(
        account_id=account_id,
        broker_id=broker_id,
        available_cash=available_cash,
        used_margin=used_margin,
        positions=tuple(positions),
        open_orders=tuple(open_orders),
        trades=tuple(trades),
    )
