"""
Phase 14.6 -- Blocker B: a single, reusable broker-response validation
layer, shared by every live-capable execution path (plain LIVE and
LIVE_CANARY alike).

Before Phase 14.6, this logic existed only inside
trading.common.live_canary.LiveCanaryGuard.validate_broker_response(),
so a plain LIVE order (no canary_guard attached) had NO sanity check on
what the broker adapter returned -- a malformed or ambiguous response
could have been trusted as a legitimate outcome. This module extracts
that logic once; StrategyExecutionEngine.execute() now calls it
unconditionally (every execution_mode, not just LIVE_CANARY), and
LiveCanaryGuard.validate_broker_response() delegates here instead of
duplicating the rules.

Never calls a broker. Only inspects the OrderResult object already
returned by a BrokerClient adapter.
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.common.broker import OrderResult

# The full order-status vocabulary any adapter in this repo produces
# (trading/common/execution.py's TERMINAL_STATUSES plus the non-terminal
# statuses adapters report while an order is still open). A status
# outside this set is refused rather than trusted, however plausible it
# looks -- an adapter bug or a broker API change should surface loudly,
# not silently pass through as "probably fine".
VALID_ORDER_STATUSES = frozenset({
    "OPEN", "COMPLETE", "FILLED", "REJECTED", "CANCELLED", "PENDING", "TRIGGER PENDING",
})

# Statuses that do not require a non-empty order_id to be considered
# valid -- a broker that refuses an order before ever creating one has
# nothing to assign an id to.
_STATUSES_ALLOWING_NO_ORDER_ID = frozenset({"REJECTED"})


@dataclass(frozen=True)
class BrokerResponseValidation:
    valid: bool
    reason: str = ""


def validate_broker_response(result: OrderResult | None) -> BrokerResponseValidation:
    """Fails closed on anything that isn't unambiguously a well-formed
    OrderResult:

    - response exists (not None)
    - response is actually an OrderResult (not some other, unexpected type)
    - status is one of VALID_ORDER_STATUSES
    - a non-REJECTED status carries a non-empty order_id
    - quantity is not negative (a broker reporting a negative quantity is
      nonsensical and must never be read as a legitimate fill)

    A "timeout" or "no response at all" case is represented by `result`
    being None (StrategyExecutionEngine's own retry loop already turns an
    exhausted-retries broker call into `order_result = None` before this
    function is ever reached) -- this function does not itself distinguish
    "broker said no" from "broker never answered"; that distinction is
    exactly why StrategyExecutionEngine treats `order_result is None` as
    its own, separate failure path (never persisted as a definitive
    idempotency outcome -- see trading/common/idempotency_store.py's
    module docstring for why a genuinely ambiguous non-response is
    handled differently from a definitive broker answer).
    """
    if result is None:
        return BrokerResponseValidation(False, "broker returned no result")
    if not isinstance(result, OrderResult):
        return BrokerResponseValidation(False, f"broker returned an unexpected type: {type(result).__name__}")
    if result.status not in VALID_ORDER_STATUSES:
        return BrokerResponseValidation(False, f"unrecognized broker order status {result.status!r}")
    if result.status not in _STATUSES_ALLOWING_NO_ORDER_ID and not result.order_id:
        return BrokerResponseValidation(False, f"broker reported status {result.status!r} with no order_id")
    if result.quantity < 0:
        return BrokerResponseValidation(False, f"broker reported a negative quantity: {result.quantity}")
    return BrokerResponseValidation(True)
