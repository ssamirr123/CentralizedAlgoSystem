"""Phase 14.6 Blocker B: trading/common/broker_response_validation.py --
the shared validator now used unconditionally by
StrategyExecutionEngine.execute() for every execution mode, and delegated
to by LiveCanaryGuard.validate_broker_response() (see
tests/common/test_live_canary.py's own check-10 tests for the
LiveCanaryGuard-facing wrapper)."""
from __future__ import annotations

from trading.common.broker import OrderResult, OrderSide
from trading.common.broker_response_validation import VALID_ORDER_STATUSES, validate_broker_response


def _result(**overrides) -> OrderResult:
    fields = dict(order_id="AO-1", symbol="NIFTY", side=OrderSide.BUY, quantity=65, status="OPEN", message="")
    fields.update(overrides)
    return OrderResult(**fields)


# 1. valid broker response
def test_valid_open_response_passes():
    outcome = validate_broker_response(_result(status="OPEN"))
    assert outcome.valid is True


def test_valid_complete_response_passes():
    outcome = validate_broker_response(_result(status="COMPLETE"))
    assert outcome.valid is True


# 2. rejected order
def test_rejected_order_with_no_id_is_valid():
    outcome = validate_broker_response(_result(order_id="", status="REJECTED", message="insufficient margin"))
    assert outcome.valid is True


# 3. null response
def test_null_response_fails_closed():
    outcome = validate_broker_response(None)
    assert outcome.valid is False
    assert "no result" in outcome.reason


# 4. malformed response (wrong type entirely)
def test_malformed_response_type_fails_closed():
    outcome = validate_broker_response({"status": "OPEN", "order_id": "AO-1"})  # type: ignore[arg-type]
    assert outcome.valid is False
    assert "unexpected type" in outcome.reason


# 5. missing order ID
def test_non_rejected_status_with_missing_order_id_fails_closed():
    outcome = validate_broker_response(_result(order_id="", status="OPEN"))
    assert outcome.valid is False
    assert "order_id" in outcome.reason


# 6. broker error response (represented as an unrecognized/error status string)
def test_broker_error_style_status_fails_closed():
    outcome = validate_broker_response(_result(status="ERROR: RMS_LIMIT_BREACH"))
    assert outcome.valid is False
    assert "unrecognized" in outcome.reason


# 7. timeout -- represented by the engine passing None once retries are
# exhausted (see execution.py); this module cannot distinguish "broker
# said no" from "broker never answered" and is not meant to.
def test_timeout_represented_as_none_fails_closed():
    outcome = validate_broker_response(None)
    assert outcome.valid is False


# 8. ambiguous response (recognized status, but internally inconsistent --
# a negative quantity is nonsensical and must never be read as a fill)
def test_negative_quantity_is_ambiguous_and_fails_closed():
    outcome = validate_broker_response(_result(status="COMPLETE", quantity=-5))
    assert outcome.valid is False
    assert "negative quantity" in outcome.reason


def test_every_valid_status_is_individually_accepted_when_well_formed():
    for status in VALID_ORDER_STATUSES:
        order_id = "" if status == "REJECTED" else "AO-1"
        outcome = validate_broker_response(_result(status=status, order_id=order_id))
        assert outcome.valid is True, f"status {status!r} unexpectedly failed: {outcome.reason}"
