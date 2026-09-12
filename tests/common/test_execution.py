"""trading/common/execution.py -- StrategyExecutionEngine exercised against
PaperBroker (real adapter) plus a minimal FakeBroker for pending-order
scenarios PaperBroker can't produce (it always fills instantly)."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from trading.common.broker import OrderResult, OrderSide, OrderType, Quote
from trading.common.brokers.paper_broker import PaperBroker
from trading.common.execution import ExecutionConfig, OrderState, StrategyExecutionEngine


class FakeBroker:
    """Duck-typed BrokerClient that stays OPEN for a configurable number of
    get_order() polls before resolving, so pending-order management and
    retry/reprice logic can be exercised deterministically."""

    def __init__(self, opens_before_fill=0, reject_first_n=0):
        self.orders: dict[str, dict] = {}
        self._next_id = 1
        self.opens_before_fill = opens_before_fill
        self.reject_first_n = reject_first_n
        self.place_calls: list[tuple] = []
        self.modify_calls: list[tuple] = []
        self.cancel_calls: list[str] = []

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, last_price=100.0, timestamp=datetime.now(timezone.utc).isoformat())

    def place_order(self, symbol, side, quantity, order_type=OrderType.MARKET, limit_price=None):
        self.place_calls.append((symbol, side, quantity, order_type, limit_price))
        attempt = len(self.place_calls)
        if attempt <= self.reject_first_n:
            return OrderResult(order_id="", symbol=symbol, side=side, quantity=quantity, status="REJECTED")
        order_id = f"FAKE-{self._next_id}"
        self._next_id += 1
        self.orders[order_id] = {"quantity": quantity, "filled": 0, "polls": 0}
        status = "COMPLETE" if self.opens_before_fill == 0 else "OPEN"
        return OrderResult(order_id=order_id, symbol=symbol, side=side, quantity=quantity, status=status)

    def get_order(self, order_id: str) -> OrderState:
        order = self.orders[order_id]
        order["polls"] += 1
        if order["polls"] >= self.opens_before_fill:
            order["filled"] = order["quantity"]
        remaining = order["quantity"] - order["filled"]
        status = "COMPLETE" if remaining == 0 else "OPEN"
        return OrderState(order_id=order_id, status=status, filled_quantity=order["filled"], remaining_quantity=remaining)

    def modify_order(self, order_id: str, quantity: int, limit_price: float) -> bool:
        self.modify_calls.append((order_id, quantity, limit_price))
        return True

    def cancel_order(self, order_id: str) -> bool:
        self.cancel_calls.append(order_id)
        return True


FAST = ExecutionConfig(pending_timeout_seconds=0.05, retry_delay_seconds=0.01, min_api_interval_seconds=0.0)


def test_place_limit_against_paper_broker_fills_immediately():
    broker = PaperBroker()
    broker.connect()
    engine = StrategyExecutionEngine(broker, FAST)

    result = engine.place_limit("NIFTY24950CE", OrderSide.BUY, 50)

    assert result.status == "FILLED"
    assert result.order_id.startswith("PAPER-")


def test_dry_run_never_calls_the_broker():
    broker = FakeBroker()
    engine = StrategyExecutionEngine(broker, ExecutionConfig(dry_run=True))

    result = engine.place_limit("NIFTY24950CE", OrderSide.BUY, 50)

    assert result.status == "FILLED"
    assert result.order_id.startswith("DRYRUN-")
    assert broker.place_calls == []


def test_retry_with_reprice_recovers_from_rejection():
    broker = FakeBroker(reject_first_n=2)
    engine = StrategyExecutionEngine(broker, FAST)

    result = engine.place_limit("NIFTY24950CE", OrderSide.SELL, 50)

    assert result is not None
    assert result.status == "COMPLETE"
    # Price should walk down on each retry for a SELL (toward the market).
    prices = [call[4] for call in broker.place_calls]
    assert prices[0] > prices[1] > prices[2]


def test_exhausted_retries_returns_none():
    broker = FakeBroker(reject_first_n=99)
    engine = StrategyExecutionEngine(broker, ExecutionConfig(max_retries=2, retry_delay_seconds=0.01, min_api_interval_seconds=0.0))

    result = engine.place_limit("NIFTY24950CE", OrderSide.BUY, 50)

    assert result is None
    assert len(broker.place_calls) == 2


def test_pending_order_cancelled_after_timeout():
    broker = FakeBroker(opens_before_fill=99)  # never fills on its own
    engine = StrategyExecutionEngine(broker, ExecutionConfig(
        pending_timeout_seconds=0.05, pending_action="CANCEL",
        retry_delay_seconds=0.01, min_api_interval_seconds=0.0,
    ))

    result = engine.place_limit("NIFTY24950CE", OrderSide.BUY, 50)
    assert result.status == "OPEN"

    time.sleep(0.2)  # let the background management thread run

    assert broker.cancel_calls == [result.order_id]


def test_pending_order_modified_with_remainder_on_partial_fill():
    broker = FakeBroker(opens_before_fill=99)  # never auto-fills; we'll force a partial fill below

    engine = StrategyExecutionEngine(broker, ExecutionConfig(
        pending_timeout_seconds=0.05, pending_action="MODIFY",
        retry_delay_seconds=0.01, min_api_interval_seconds=0.0,
    ))

    result = engine.place_limit("NIFTY24950CE", OrderSide.BUY, 50)
    order_id = result.order_id
    broker.orders[order_id]["filled"] = 20  # simulate a partial fill

    time.sleep(0.2)

    assert broker.modify_calls, "expected modify_order to be called for the unfilled remainder"
    modified_id, modified_qty, _price = broker.modify_calls[0]
    assert modified_id == order_id
    assert modified_qty == 30  # 50 - 20 filled
    assert broker.cancel_calls == []


def test_market_emergency_falls_back_to_limit_when_disabled():
    broker = FakeBroker()
    engine = StrategyExecutionEngine(broker, ExecutionConfig(
        allow_market_emergency=False, pending_timeout_seconds=0.05,
        retry_delay_seconds=0.01, min_api_interval_seconds=0.0,
    ))

    engine.place_market_emergency("NIFTY24950CE", OrderSide.SELL, 50)

    assert broker.place_calls[0][3] == OrderType.LIMIT


def test_market_emergency_places_market_order_when_enabled():
    broker = FakeBroker()
    engine = StrategyExecutionEngine(broker, ExecutionConfig(
        allow_market_emergency=True, pending_timeout_seconds=0.05,
        retry_delay_seconds=0.01, min_api_interval_seconds=0.0,
    ))

    engine.place_market_emergency("NIFTY24950CE", OrderSide.SELL, 50)

    assert broker.place_calls[0][3] == OrderType.MARKET


def test_cancel_all_pending_cancels_every_tracked_order():
    broker = FakeBroker(opens_before_fill=99)
    engine = StrategyExecutionEngine(broker, ExecutionConfig(
        pending_timeout_seconds=100, retry_delay_seconds=0.01, min_api_interval_seconds=0.0,
    ))

    r1 = engine.place_limit("NIFTY24950CE", OrderSide.BUY, 50)
    r2 = engine.place_limit("NIFTY24950PE", OrderSide.SELL, 50)

    engine.cancel_all_pending()

    assert set(broker.cancel_calls) == {r1.order_id, r2.order_id}
