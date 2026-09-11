"""
RiskManager -- the pre-trade interception point every OrderIntent must pass
through before it reaches the ExecutionEngine.

Phase 1 is deliberately a skeleton: only structural/mechanical checks
(strategy enabled, assignment exists, account exists+enabled, positive
quantity, valid side/order-type, a limit price present for LIMIT orders).
Moving real strategy-specific risk logic (DoubleStraddelAlgo's
DAILY_MAX_LOSS kill switch, CombinedVwapNifty's combined CE+PE risk
ladder, ...) out of the algos and into this layer is explicitly future
work -- nothing in this file changes any live risk behavior, and no live
algo calls it today.
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.common.broker import OrderSide, OrderType
from trading.common.order_intent import OrderIntent
from trading.common.strategy_assignment import (
    InvalidAssignmentError,
    StrategyAssignment,
    UnknownAssignmentError,
)


@dataclass(frozen=True)
class RiskCheckResult:
    allowed: bool
    reason: str = ""

    @classmethod
    def allow(cls) -> "RiskCheckResult":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str) -> "RiskCheckResult":
        return cls(allowed=False, reason=reason)


@dataclass(frozen=True)
class RiskContext:
    """Whatever the caller already knows and doesn't want re-derived here.
    Phase 1 only uses strategy_enabled; later phases can add day P&L,
    open-position counts, etc. without changing validate()'s signature."""

    strategy_enabled: bool = True


class RiskManager:
    def __init__(self, strategy_assignment: StrategyAssignment) -> None:
        self._strategy_assignment = strategy_assignment

    def validate(self, intent: OrderIntent, context: RiskContext | None = None) -> RiskCheckResult:
        context = context or RiskContext()

        if not context.strategy_enabled:
            return RiskCheckResult.deny(f"strategy '{intent.strategy_id}' is disabled")

        try:
            self._strategy_assignment.validate(intent.strategy_id)
        except UnknownAssignmentError as exc:
            return RiskCheckResult.deny(str(exc))
        except InvalidAssignmentError as exc:
            return RiskCheckResult.deny(str(exc))

        if intent.quantity <= 0:
            return RiskCheckResult.deny("quantity must be positive")

        if not isinstance(intent.side, OrderSide):
            return RiskCheckResult.deny(f"invalid order side: {intent.side!r}")

        if not isinstance(intent.order_type, OrderType):
            return RiskCheckResult.deny(f"invalid order type: {intent.order_type!r}")

        if intent.order_type == OrderType.LIMIT and (intent.limit_price is None or intent.limit_price <= 0):
            return RiskCheckResult.deny("LIMIT order requires a positive limit_price")

        return RiskCheckResult.allow()
