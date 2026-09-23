"""
Pure aggregation over already-persisted AIBacktestRun cells. No I/O, no
TradingAgents import, no LLM call -- every number here is a plain
arithmetic reduction over evaluation fields upstream itself already
computed (raw_return/alpha_return via TradingAgentsGraph.settle_pending,
normalized_decision via upstream's own parse_rating()).

`_DIRECTION` is copied verbatim from tradingagents.backtest._DIRECTION
(pinned v0.5.0) -- the same "what does this rating claim will happen"
mapping upstream's own summarize() uses, so our hit-rate figure means the
same thing upstream's does. Not a reimplementation of anything nontrivial;
it is five literal values.

Section 16: this module computes DECISION-QUALITY metrics only (directional
accuracy, mean/median alpha, decision counts) -- never a portfolio P&L,
CAGR, Sharpe ratio, or max drawdown, because no capital-allocation/
position-sizing simulation exists in this codebase. Every returned dict is
labeled "AI Research Decision Evaluation", never "Trading Strategy
Performance" (BacktestMetricsOut.label).
"""
from __future__ import annotations

from statistics import median

# What each rating claims will happen (Buy/Overweight => price should rise,
# Underweight/Sell => price should fall, Hold => no direction claimed, so
# nothing about alpha proves a Hold right or wrong). Mirrors
# tradingagents.backtest._DIRECTION exactly.
DIRECTION: dict[str, int] = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}

REVIEW = "REVIEW"


def compute_metrics(cells: list, *, holding_days: int) -> dict:
    """`cells` are AIBacktestRun ORM rows (or any object with the same
    attribute names: status, normalized_decision, alpha_return).
    Returns a plain dict matching BacktestMetricsOut's fields."""
    completed = [c for c in cells if c.status == "COMPLETED"]

    counts_by_decision: dict[str, int] = {}
    for c in completed:
        key = c.normalized_decision or REVIEW
        counts_by_decision[key] = counts_by_decision.get(key, 0) + 1

    resolved = [c for c in completed if c.alpha_return is not None]
    unscored = sum(1 for c in completed if (c.normalized_decision or REVIEW) == REVIEW)
    pending_evaluation = len(completed) - len(resolved) - unscored

    by_decision: dict[str, dict] = {}
    for rating in sorted({c.normalized_decision for c in resolved if c.normalized_decision}):
        alphas = [c.alpha_return for c in resolved if c.normalized_decision == rating]
        direction = DIRECTION.get(rating, 0)
        hit_rate = (sum(1 for a in alphas if a * direction > 0) / len(alphas)) if direction and alphas else None
        by_decision[rating] = {
            "count": len(alphas),
            "hit_rate": hit_rate,
            "mean_alpha": sum(alphas) / len(alphas) if alphas else 0.0,
        }

    all_alphas = [c.alpha_return for c in resolved]
    overall_mean_alpha = sum(all_alphas) / len(all_alphas) if all_alphas else None
    overall_median_alpha = median(all_alphas) if all_alphas else None
    directional_cells = [c for c in resolved if DIRECTION.get(c.normalized_decision or "", 0) != 0]
    positive_decision_rate = (
        sum(1 for c in directional_cells if c.alpha_return * DIRECTION[c.normalized_decision] > 0) / len(directional_cells)
        if directional_cells else None
    )

    return {
        "label": "AI Research Decision Evaluation",
        "total_decisions": len(completed),
        "counts_by_decision": counts_by_decision,
        "resolved": len(resolved),
        "pending_evaluation": pending_evaluation,
        "unscored": unscored,
        "by_decision": by_decision,
        "overall_mean_alpha": overall_mean_alpha,
        "overall_median_alpha": overall_median_alpha,
        "positive_decision_rate": positive_decision_rate,
        "holding_days": holding_days,
    }
