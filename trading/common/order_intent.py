"""
Broker-independent trading intent.

A strategy emits an OrderIntent describing WHAT it wants done -- never HOW
that gets submitted to a specific broker. Everything below this object
(RiskManager, ExecutionEngine, TradingAccount, BrokerManager, BrokerClient)
translates it into a broker-specific call; the object itself must never
carry an SDK type, a broker request shape, or a broker-specific field name.

Reuses OrderSide/OrderType from trading.common.broker rather than
redeclaring them, so an OrderIntent's side/order_type are drop-in
compatible with BrokerClient.place_order()'s existing parameters.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from trading.common.broker import OrderSide, OrderType
from trading.common.instrument import Instrument


class ProductType(str, Enum):
    INTRADAY = "INTRADAY"
    DELIVERY = "DELIVERY"
    MARGIN = "MARGIN"


class Validity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"


@dataclass(frozen=True)
class OrderIntent:
    """What a strategy wants done -- nothing here names a broker.

    account_id is informational/audit metadata set by whoever built the
    intent; it is NOT the authoritative routing decision. StrategyAssignment
    (strategy_id -> account_id) is the source of truth ExecutionEngine.execute()
    actually resolves against, so a strategy can't route around its
    assignment by setting account_id itself.
    """

    strategy_id: str
    account_id: str
    symbol: str
    exchange: str
    side: OrderSide
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    trigger_price: float | None = None
    product_type: ProductType = ProductType.INTRADAY
    validity: Validity = Validity.DAY
    reason: str = ""
    client_order_id: str = field(default_factory=lambda: f"OI-{uuid.uuid4().hex[:12]}")
    metadata: dict[str, Any] = field(default_factory=dict)
    # Optional structured instrument reference (see trading.common.instrument) --
    # symbol/exchange above remain the primary, always-populated fields;
    # this is an additional, optional richer identity when the caller has one.
    instrument: Instrument | None = None
    # correlation_id ties this intent to a broader unit of work (e.g. a
    # multi-leg entry) across logs/systems; defaults to a fresh id per
    # intent since most callers don't have a broader correlation to supply.
    correlation_id: str = field(default_factory=lambda: f"CORR-{uuid.uuid4().hex[:12]}")
    # idempotency_key is deliberately NOT auto-generated: it only means
    # something when the CALLER derives it deterministically from "what
    # makes two intents the same request" (e.g. strategy_id+symbol+side+a
    # time bucket). Empty string means "no idempotency dedup requested".
    # Phase 1 only carries this field -- no component yet reads or enforces
    # it; that is future ExecutionEngine/RiskManager work, not implemented
    # here.
    idempotency_key: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        # Accept either the enum member or its plain string value (e.g.
        # side="SELL") so callers don't have to import the enum just to
        # construct an intent -- but always store the enum internally.
        object.__setattr__(self, "side", OrderSide(self.side))
        object.__setattr__(self, "order_type", OrderType(self.order_type))
        object.__setattr__(self, "product_type", ProductType(self.product_type))
        object.__setattr__(self, "validity", Validity(self.validity))
