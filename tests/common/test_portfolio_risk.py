"""Phase 16.10: trading/common/portfolio_risk.py -- PortfolioRiskManager in
isolation (no WorkerCoordinator involved). Proves strategy/account/portfolio
exposure and daily-loss aggregation, order-count/open-order limits,
conflict classification, concurrency-safe reservations, and fail-closed
behavior on missing/malformed risk state."""
from __future__ import annotations

import threading

from trading.common.broker import OrderSide, OrderType
from trading.common.execution import ExecutionResult
from trading.common.order_intent import OrderIntent
from trading.common.portfolio_risk import (
    REASON_ACCOUNT_EXPOSURE_LIMIT,
    REASON_ACCOUNT_LOSS_LIMIT,
    REASON_ALLOWED,
    REASON_INVALID_RISK_STATE,
    REASON_OPEN_ORDER_LIMIT,
    REASON_ORDER_COUNT_LIMIT,
    REASON_PORTFOLIO_EXPOSURE_LIMIT,
    REASON_PORTFOLIO_LOSS_LIMIT,
    REASON_RISK_DATA_UNAVAILABLE,
    REASON_STRATEGY_EXPOSURE_LIMIT,
    REASON_STRATEGY_LOSS_LIMIT,
    ConflictType,
    PortfolioRiskLimits,
    PortfolioRiskManager,
)


def _intent(strategy_id="S1", account_id="A1", symbol="NIFTY", side=OrderSide.BUY, quantity=10, limit_price=100.0, key="k1"):
    return OrderIntent(
        strategy_id=strategy_id, account_id=account_id, symbol=symbol, exchange="NFO",
        side=side, quantity=quantity, order_type=OrderType.LIMIT, limit_price=limit_price,
        idempotency_key=key,
    )


def _filled(status="COMPLETE", success=True):
    return ExecutionResult(success=success, order_id="O1", status=status, filled_quantity=10)


# --------------------------------------------------------------------------- #
# Strategy-level exposure
# --------------------------------------------------------------------------- #
def test_strategy_exposure_below_limit_is_allowed():
    mgr = PortfolioRiskManager(strategy_limits={"S1": PortfolioRiskLimits(max_strategy_exposure=2000.0)})
    decision = mgr.evaluate_and_reserve(_intent(quantity=10, limit_price=100.0), strategy_id="S1", account_id="A1")
    assert decision.allowed is True
    assert decision.reason_code == REASON_ALLOWED


def test_strategy_exposure_exactly_at_limit_is_allowed():
    mgr = PortfolioRiskManager(strategy_limits={"S1": PortfolioRiskLimits(max_strategy_exposure=1000.0)})
    decision = mgr.evaluate_and_reserve(_intent(quantity=10, limit_price=100.0), strategy_id="S1", account_id="A1")
    assert decision.allowed is True  # projected == limit -- inclusive, matches RiskManager's own `>` (not `>=`)


def test_strategy_exposure_above_limit_is_rejected():
    mgr = PortfolioRiskManager(strategy_limits={"S1": PortfolioRiskLimits(max_strategy_exposure=999.0)})
    decision = mgr.evaluate_and_reserve(_intent(quantity=10, limit_price=100.0), strategy_id="S1", account_id="A1")
    assert decision.allowed is False
    assert decision.reason_code == REASON_STRATEGY_EXPOSURE_LIMIT


def test_strategy_daily_loss_limit_blocks_after_a_realized_loss():
    mgr = PortfolioRiskManager(strategy_limits={"S1": PortfolioRiskLimits(max_strategy_daily_loss=50.0)})
    # Open long 10 @ 100, then close it at 90 -> realized loss of 100.
    d1 = mgr.evaluate_and_reserve(_intent(side=OrderSide.BUY, quantity=10, limit_price=100.0, key="open"), strategy_id="S1", account_id="A1")
    mgr.commit_reservation(d1.reservation_id, execution_result=_filled())
    d2 = mgr.evaluate_and_reserve(_intent(side=OrderSide.SELL, quantity=10, limit_price=90.0, key="close"), strategy_id="S1", account_id="A1")
    mgr.commit_reservation(d2.reservation_id, execution_result=_filled())

    d3 = mgr.evaluate_and_reserve(_intent(side=OrderSide.BUY, quantity=1, limit_price=100.0, key="next"), strategy_id="S1", account_id="A1")
    assert d3.allowed is False
    assert d3.reason_code == REASON_STRATEGY_LOSS_LIMIT


def test_strategy_order_count_limit():
    mgr = PortfolioRiskManager(strategy_limits={"S1": PortfolioRiskLimits(max_strategy_orders_per_day=1)})
    d1 = mgr.evaluate_and_reserve(_intent(key="a"), strategy_id="S1", account_id="A1")
    assert d1.allowed is True
    d2 = mgr.evaluate_and_reserve(_intent(key="b"), strategy_id="S1", account_id="A1")
    assert d2.allowed is False
    assert d2.reason_code == REASON_ORDER_COUNT_LIMIT


# --------------------------------------------------------------------------- #
# Account-level: multiple strategies sharing one account
# --------------------------------------------------------------------------- #
def test_account_exposure_aggregates_across_strategies():
    mgr = PortfolioRiskManager(account_limits={"ACC": PortfolioRiskLimits(max_account_exposure=1500.0)})
    d1 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", account_id="ACC", quantity=10, limit_price=100.0, key="a"), strategy_id="S1", account_id="ACC")
    assert d1.allowed is True  # 1000 <= 1500
    d2 = mgr.evaluate_and_reserve(_intent(strategy_id="S2", account_id="ACC", quantity=6, limit_price=100.0, key="b"), strategy_id="S2", account_id="ACC")
    assert d2.allowed is False  # 1000 + 600 = 1600 > 1500
    assert d2.reason_code == REASON_ACCOUNT_EXPOSURE_LIMIT


def test_account_daily_loss_limit_blocks_a_different_strategy_on_the_same_account():
    mgr = PortfolioRiskManager(account_limits={"ACC": PortfolioRiskLimits(max_account_daily_loss=50.0)})
    d1 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", account_id="ACC", side=OrderSide.BUY, quantity=10, limit_price=100.0, key="open"), strategy_id="S1", account_id="ACC")
    mgr.commit_reservation(d1.reservation_id, execution_result=_filled())
    d2 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", account_id="ACC", side=OrderSide.SELL, quantity=10, limit_price=90.0, key="close"), strategy_id="S1", account_id="ACC")
    mgr.commit_reservation(d2.reservation_id, execution_result=_filled())

    d3 = mgr.evaluate_and_reserve(_intent(strategy_id="S2", account_id="ACC", quantity=1, limit_price=100.0, key="s2-order"), strategy_id="S2", account_id="ACC")
    assert d3.allowed is False
    assert d3.reason_code == REASON_ACCOUNT_LOSS_LIMIT


def test_account_order_count_limit_shared_across_strategies():
    mgr = PortfolioRiskManager(account_limits={"ACC": PortfolioRiskLimits(max_account_orders_per_day=1)})
    d1 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", account_id="ACC", key="a"), strategy_id="S1", account_id="ACC")
    assert d1.allowed is True
    d2 = mgr.evaluate_and_reserve(_intent(strategy_id="S2", account_id="ACC", key="b"), strategy_id="S2", account_id="ACC")
    assert d2.allowed is False
    assert d2.reason_code == REASON_ORDER_COUNT_LIMIT


# --------------------------------------------------------------------------- #
# Portfolio-level: multiple accounts
# --------------------------------------------------------------------------- #
def test_portfolio_exposure_aggregates_across_accounts():
    mgr = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=1500.0))
    d1 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", account_id="A1", quantity=10, limit_price=100.0, key="a"), strategy_id="S1", account_id="A1")
    assert d1.allowed is True
    d2 = mgr.evaluate_and_reserve(_intent(strategy_id="S2", account_id="A2", quantity=6, limit_price=100.0, key="b"), strategy_id="S2", account_id="A2")
    assert d2.allowed is False
    assert d2.reason_code == REASON_PORTFOLIO_EXPOSURE_LIMIT


def test_portfolio_daily_loss_limit_blocks_every_account():
    mgr = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_daily_loss=50.0))
    d1 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", account_id="A1", side=OrderSide.BUY, quantity=10, limit_price=100.0, key="open"), strategy_id="S1", account_id="A1")
    mgr.commit_reservation(d1.reservation_id, execution_result=_filled())
    d2 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", account_id="A1", side=OrderSide.SELL, quantity=10, limit_price=90.0, key="close"), strategy_id="S1", account_id="A1")
    mgr.commit_reservation(d2.reservation_id, execution_result=_filled())

    d3 = mgr.evaluate_and_reserve(_intent(strategy_id="S2", account_id="A2", quantity=1, limit_price=100.0, key="other-account"), strategy_id="S2", account_id="A2")
    assert d3.allowed is False
    assert d3.reason_code == REASON_PORTFOLIO_LOSS_LIMIT


def test_portfolio_order_count_limit_shared_across_everything():
    mgr = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_orders_per_day=1))
    d1 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", account_id="A1", key="a"), strategy_id="S1", account_id="A1")
    assert d1.allowed is True
    d2 = mgr.evaluate_and_reserve(_intent(strategy_id="S2", account_id="A2", key="b"), strategy_id="S2", account_id="A2")
    assert d2.allowed is False
    assert d2.reason_code == REASON_ORDER_COUNT_LIMIT


def test_strategy_and_account_and_portfolio_limits_are_independent():
    # Passing strategy risk does not imply passing account risk, and
    # passing account risk does not imply passing portfolio risk.
    mgr = PortfolioRiskManager(
        strategy_limits={"S1": PortfolioRiskLimits(max_strategy_exposure=10_000.0)},
        account_limits={"A1": PortfolioRiskLimits(max_account_exposure=10_000.0)},
        portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=500.0),
    )
    decision = mgr.evaluate_and_reserve(_intent(quantity=10, limit_price=100.0), strategy_id="S1", account_id="A1")
    assert decision.allowed is False
    assert decision.reason_code == REASON_PORTFOLIO_EXPOSURE_LIMIT


# --------------------------------------------------------------------------- #
# Open order limits
# --------------------------------------------------------------------------- #
def test_open_order_limit_blocks_once_reached():
    mgr = PortfolioRiskManager(strategy_limits={"S1": PortfolioRiskLimits(max_strategy_open_orders=1)})
    d1 = mgr.evaluate_and_reserve(_intent(key="a"), strategy_id="S1", account_id="A1")
    mgr.commit_reservation(d1.reservation_id, execution_result=ExecutionResult(success=True, order_id="O1", status="OPEN"))

    d2 = mgr.evaluate_and_reserve(_intent(key="b"), strategy_id="S1", account_id="A1")
    assert d2.allowed is False
    assert d2.reason_code == REASON_OPEN_ORDER_LIMIT


def test_resolve_open_order_frees_up_the_slot():
    mgr = PortfolioRiskManager(strategy_limits={"S1": PortfolioRiskLimits(max_strategy_open_orders=1)})
    d1 = mgr.evaluate_and_reserve(_intent(key="a"), strategy_id="S1", account_id="A1")
    mgr.commit_reservation(d1.reservation_id, execution_result=ExecutionResult(success=True, order_id="O1", status="OPEN"))
    mgr.resolve_open_order(d1.reservation_id)

    d2 = mgr.evaluate_and_reserve(_intent(key="b"), strategy_id="S1", account_id="A1")
    assert d2.allowed is True


# --------------------------------------------------------------------------- #
# Data safety
# --------------------------------------------------------------------------- #
def test_missing_price_with_exposure_limit_configured_is_risk_data_unavailable():
    mgr = PortfolioRiskManager(strategy_limits={"S1": PortfolioRiskLimits(max_strategy_exposure=1000.0)})
    intent = _intent(limit_price=None)
    decision = mgr.evaluate_and_reserve(intent, strategy_id="S1", account_id="A1")
    assert decision.allowed is False
    assert decision.reason_code == REASON_RISK_DATA_UNAVAILABLE


def test_missing_price_with_no_exposure_limit_configured_is_allowed():
    mgr = PortfolioRiskManager()  # no limits at all
    intent = _intent(limit_price=None)
    decision = mgr.evaluate_and_reserve(intent, strategy_id="S1", account_id="A1")
    assert decision.allowed is True


def test_internal_exception_fails_closed_not_silently_zero():
    mgr = PortfolioRiskManager(clock=lambda: (_ for _ in ()).throw(RuntimeError("clock broke")))
    decision = mgr.evaluate_and_reserve(_intent(), strategy_id="S1", account_id="A1")
    assert decision.allowed is False
    assert decision.reason_code == REASON_INVALID_RISK_STATE


def test_unknown_pnl_is_never_silently_treated_as_zero_risk():
    # A strategy/account/portfolio that has never traded legitimately has
    # zero realized P&L (nothing has closed) -- confirm that is reported
    # honestly as 0.0, distinct from the fail-closed RISK_DATA_UNAVAILABLE/
    # INVALID_RISK_STATE paths above, which fire only when state truly
    # cannot be determined (missing price, internal error).
    mgr = PortfolioRiskManager()
    snapshot = mgr.get_snapshot(strategy_id="NEVER_TRADED", account_id="NEVER_TRADED")
    assert snapshot.strategy_daily_pnl == 0.0
    assert snapshot.account_daily_pnl == 0.0


# --------------------------------------------------------------------------- #
# get_snapshot() is a pure read (Phase 15D.6 discipline)
# --------------------------------------------------------------------------- #
def test_get_snapshot_never_mutates_state():
    mgr = PortfolioRiskManager(strategy_limits={"S1": PortfolioRiskLimits(max_strategy_orders_per_day=1)})
    for _ in range(5):
        mgr.get_snapshot(strategy_id="S1", account_id="A1")
    # If get_snapshot() had any side effect on order counts, this would now be rejected.
    decision = mgr.evaluate_and_reserve(_intent(), strategy_id="S1", account_id="A1")
    assert decision.allowed is True


# --------------------------------------------------------------------------- #
# Conflict detection
# --------------------------------------------------------------------------- #
def test_non_conflicting_intents_report_none():
    mgr = PortfolioRiskManager()
    decision = mgr.evaluate_and_reserve(_intent(strategy_id="S1", symbol="NIFTY"), strategy_id="S1", account_id="A1")
    assert any(c.conflict_type == ConflictType.NONE for c in decision.conflicts)


def test_duplicate_same_direction_same_strategy_is_classified_not_blocked():
    mgr = PortfolioRiskManager()
    d1 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", side=OrderSide.BUY, key="a"), strategy_id="S1", account_id="A1")
    mgr.commit_reservation(d1.reservation_id, execution_result=_filled())

    d2 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", side=OrderSide.BUY, key="b"), strategy_id="S1", account_id="A1")
    assert d2.allowed is True  # informational only, never blocked by default
    assert any(c.conflict_type == ConflictType.DUPLICATE for c in d2.conflicts)


def test_opposing_different_strategies_is_classified_as_hedge_metadata_not_blocked():
    mgr = PortfolioRiskManager()
    d1 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", side=OrderSide.SELL, symbol="NIFTY25000CE", key="a"), strategy_id="S1", account_id="A1")
    mgr.commit_reservation(d1.reservation_id, execution_result=_filled())

    d2 = mgr.evaluate_and_reserve(_intent(strategy_id="S2", side=OrderSide.BUY, symbol="NIFTY25000CE", key="b"), strategy_id="S2", account_id="A2")
    assert d2.allowed is True  # legitimate hedge -- never auto-blocked
    assert any(c.conflict_type == ConflictType.OPPOSING and c.counter_strategy_id == "S1" for c in d2.conflicts)


def test_self_offsetting_is_classified_when_reducing_own_position():
    mgr = PortfolioRiskManager()
    d1 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", side=OrderSide.BUY, key="a"), strategy_id="S1", account_id="A1")
    mgr.commit_reservation(d1.reservation_id, execution_result=_filled())

    d2 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", side=OrderSide.SELL, key="b"), strategy_id="S1", account_id="A1")
    assert any(c.conflict_type == ConflictType.SELF_OFFSETTING for c in d2.conflicts)


def test_concentration_is_flagged_when_one_symbol_dominates_portfolio():
    mgr = PortfolioRiskManager(concentration_warn_ratio=0.75)
    d1 = mgr.evaluate_and_reserve(_intent(strategy_id="S1", symbol="OTHER", quantity=1, limit_price=10.0, key="small"), strategy_id="S1", account_id="A1")
    mgr.commit_reservation(d1.reservation_id, execution_result=_filled())

    d2 = mgr.evaluate_and_reserve(_intent(strategy_id="S2", symbol="NIFTY", quantity=100, limit_price=100.0, key="big"), strategy_id="S2", account_id="A2")
    assert any(c.conflict_type == ConflictType.CONCENTRATION for c in d2.conflicts)


def test_concentration_is_unknown_when_price_unavailable_and_no_exposure_limit():
    mgr = PortfolioRiskManager()
    decision = mgr.evaluate_and_reserve(_intent(limit_price=None), strategy_id="S1", account_id="A1")
    assert any(c.conflict_type == ConflictType.UNKNOWN for c in decision.conflicts)


def test_hard_block_conflicts_can_be_opted_in_but_default_is_empty():
    from trading.common.portfolio_risk import PortfolioRiskManager as PRM

    default_mgr = PRM()
    assert default_mgr._hard_block_conflicts == frozenset()

    blocking_mgr = PRM(hard_block_conflicts=frozenset({ConflictType.OPPOSING}))
    d1 = blocking_mgr.evaluate_and_reserve(_intent(strategy_id="S1", side=OrderSide.SELL, symbol="X", key="a"), strategy_id="S1", account_id="A1")
    blocking_mgr.commit_reservation(d1.reservation_id, execution_result=_filled())
    d2 = blocking_mgr.evaluate_and_reserve(_intent(strategy_id="S2", side=OrderSide.BUY, symbol="X", key="b"), strategy_id="S2", account_id="A2")
    assert d2.allowed is False


# --------------------------------------------------------------------------- #
# Idempotency: reservation not duplicated by a same-intent retry
# --------------------------------------------------------------------------- #
def test_committing_the_same_reservation_twice_is_a_safe_no_op():
    mgr = PortfolioRiskManager(strategy_limits={"S1": PortfolioRiskLimits(max_strategy_exposure=1000.0)})
    d1 = mgr.evaluate_and_reserve(_intent(quantity=10, limit_price=100.0, key="a"), strategy_id="S1", account_id="A1")
    mgr.commit_reservation(d1.reservation_id, execution_result=_filled())
    mgr.commit_reservation(d1.reservation_id, execution_result=_filled())  # second call: reservation already popped

    snapshot = mgr.get_snapshot(strategy_id="S1", account_id="A1")
    assert snapshot.strategy_exposure == 1000.0  # not double-counted


def test_releasing_an_unknown_reservation_is_a_safe_no_op():
    mgr = PortfolioRiskManager()
    mgr.release_reservation("does-not-exist")  # must not raise


# --------------------------------------------------------------------------- #
# Concurrency safety (Section 19/20)
# --------------------------------------------------------------------------- #
def test_concurrent_reservations_cannot_jointly_exceed_the_portfolio_limit():
    mgr = PortfolioRiskManager(portfolio_limits=PortfolioRiskLimits(max_portfolio_exposure=500_000.0))
    # Seed committed exposure of 400,000.
    seed = mgr.evaluate_and_reserve(_intent(strategy_id="SEED", account_id="A0", quantity=4000, limit_price=100.0, key="seed"), strategy_id="SEED", account_id="A0")
    mgr.commit_reservation(seed.reservation_id, execution_result=_filled())

    results: list = [None, None]

    def worker(i: int, strategy_id: str, account_id: str, key: str) -> None:
        intent = _intent(strategy_id=strategy_id, account_id=account_id, quantity=750, limit_price=100.0, key=key)
        results[i] = mgr.evaluate_and_reserve(intent, strategy_id=strategy_id, account_id=account_id)

    t1 = threading.Thread(target=worker, args=(0, "WA", "AA", "wa"))
    t2 = threading.Thread(target=worker, args=(1, "WB", "AB", "wb"))
    t1.start(); t2.start()
    t1.join(); t2.join()

    accepted = [r for r in results if r.allowed]
    rejected = [r for r in results if not r.allowed]
    # 400k + 75k + 75k = 550k > 500k -- both cannot be accepted.
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0].reason_code == REASON_PORTFOLIO_EXPOSURE_LIMIT


def test_released_reservation_does_not_permanently_consume_order_count_budget():
    mgr = PortfolioRiskManager(strategy_limits={"S1": PortfolioRiskLimits(max_strategy_orders_per_day=1)})
    d1 = mgr.evaluate_and_reserve(_intent(key="a"), strategy_id="S1", account_id="A1")
    assert d1.allowed is True
    mgr.release_reservation(d1.reservation_id)  # e.g. rejected further downstream by the existing RiskManager

    d2 = mgr.evaluate_and_reserve(_intent(key="b"), strategy_id="S1", account_id="A1")
    assert d2.allowed is True  # budget was given back, not permanently consumed
