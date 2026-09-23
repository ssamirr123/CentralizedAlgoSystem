"""Section 15/16: decision-evaluation metrics -- never a portfolio P&L figure."""
from __future__ import annotations

from dataclasses import dataclass

from trading.ai_research.backtest import evaluator


@dataclass
class _Cell:
    status: str
    normalized_decision: str | None
    alpha_return: float | None


def test_empty_cells_produce_zeroed_metrics():
    metrics = evaluator.compute_metrics([], holding_days=5)
    assert metrics["label"] == "AI Research Decision Evaluation"
    assert metrics["total_decisions"] == 0
    assert metrics["resolved"] == 0
    assert metrics["overall_mean_alpha"] is None


def test_failed_cells_excluded_from_decision_counts():
    cells = [_Cell("FAILED", None, None), _Cell("COMPLETED", "Buy", 0.02)]
    metrics = evaluator.compute_metrics(cells, holding_days=5)
    assert metrics["total_decisions"] == 1


def test_review_counted_as_unscored_not_a_direction():
    cells = [_Cell("COMPLETED", "REVIEW", None)]
    metrics = evaluator.compute_metrics(cells, holding_days=5)
    assert metrics["unscored"] == 1
    assert metrics["by_decision"] == {}


def test_hold_has_no_hit_rate_since_it_claims_no_direction():
    cells = [_Cell("COMPLETED", "Hold", 0.01), _Cell("COMPLETED", "Hold", -0.02)]
    metrics = evaluator.compute_metrics(cells, holding_days=5)
    assert metrics["by_decision"]["Hold"]["hit_rate"] is None
    assert metrics["by_decision"]["Hold"]["count"] == 2


def test_buy_hit_rate_counts_positive_alpha_as_a_hit():
    cells = [
        _Cell("COMPLETED", "Buy", 0.03),   # correct direction
        _Cell("COMPLETED", "Buy", -0.01),  # wrong direction
    ]
    metrics = evaluator.compute_metrics(cells, holding_days=5)
    assert metrics["by_decision"]["Buy"]["count"] == 2
    assert metrics["by_decision"]["Buy"]["hit_rate"] == 0.5


def test_sell_hit_rate_counts_negative_alpha_as_a_hit():
    cells = [_Cell("COMPLETED", "Sell", -0.02), _Cell("COMPLETED", "Sell", 0.01)]
    metrics = evaluator.compute_metrics(cells, holding_days=5)
    assert metrics["by_decision"]["Sell"]["hit_rate"] == 0.5


def test_pending_evaluation_counted_separately_from_resolved():
    cells = [_Cell("COMPLETED", "Buy", None), _Cell("COMPLETED", "Buy", 0.01)]
    metrics = evaluator.compute_metrics(cells, holding_days=5)
    assert metrics["resolved"] == 1
    assert metrics["pending_evaluation"] == 1


def test_metrics_never_contain_portfolio_pnl_fields():
    """Section 16: no Sharpe/CAGR/drawdown/portfolio-P&L key of any kind."""
    cells = [_Cell("COMPLETED", "Buy", 0.02)]
    metrics = evaluator.compute_metrics(cells, holding_days=5)
    forbidden = ("sharpe", "cagr", "drawdown", "pnl", "portfolio_value", "equity_curve")
    keys = set(metrics.keys())
    assert not any(f in k.lower() for k in keys for f in forbidden)
