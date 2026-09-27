"""
Monthly momentum rotation -- the same rebalancing sequence as the backtest
(ETF_Momentum_Test_1.py), applied to a persistent Portfolio.

Pure logic, no I/O: takes a DataFrame of daily closes (index = dates,
columns = NSE symbols, forward-filled, never back-filled) and mutates the
portfolio, returning the orders it filled. Paper fills happen at the
rebalance day's close, exactly as the backtest assumes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd

from trading.algos.ETF_Momentum.config import (
    CATEGORY, DMA_PERIOD, LB, LIQUID_RATE, MIN_HISTORY, N_HOLD, SL_PCT, TRAIL_PCT, WEIGHTS,
)
from trading.algos.ETF_Momentum.portfolio import Portfolio


@dataclass(frozen=True)
class Order:
    side: str             # "BUY" / "SELL"
    symbol: str
    qty: int
    price: float
    reason: str           # "ENTRY" / "SL" / "Trail" / "Rotation"
    score: float | None = None
    pnl_inr: float | None = None


@dataclass(frozen=True)
class RebalanceResult:
    as_of: date
    orders: list[Order]
    top: list[str]
    skipped_below_dma: list[str]
    interest: float
    value_after: float


def momentum_scores(closes: pd.DataFrame) -> dict[str, float]:
    """Score = 0.15*ret_1m + 0.40*ret_3m + 0.30*ret_6m + 0.15*ret_12m (in %),
    for every ETF with at least MIN_HISTORY rows. "N trading days ago" is
    positional: iloc[-(N+1)] with today at iloc[-1]."""
    scores = {}
    for sym in closes.columns:
        s = closes[sym].dropna()
        if len(s) < MIN_HISTORY:
            continue
        p0 = s.iloc[-1]
        rets = [(p0 - s.iloc[-(lb + 1)]) / s.iloc[-(lb + 1)] * 100 for lb in LB]
        scores[sym] = sum(w * r for w, r in zip(WEIGHTS, rets))
    return scores


def above_dma(series: pd.Series) -> bool:
    s = series.dropna()
    return len(s) > 0 and s.iloc[-1] > s.iloc[-DMA_PERIOD:].mean()


def is_first_trading_day_of_month(index: pd.DatetimeIndex, day: date) -> bool:
    ts = pd.Timestamp(day)
    if ts not in index:
        return False
    same_month = index[(index.year == ts.year) & (index.month == ts.month)]
    return same_month[0] == ts


def rebalance(portfolio: Portfolio, closes: pd.DataFrame, as_of: date) -> RebalanceResult:
    """Run one rebalance at as_of's close. closes must contain as_of."""
    closes = closes.loc[: pd.Timestamp(as_of)]
    if closes.empty or closes.index[-1] != pd.Timestamp(as_of):
        raise ValueError(f"no close for {as_of} in price data")
    last = closes.iloc[-1]

    # Step 1 -- accrue liquid interest on idle cash
    interest = portfolio.accrue_interest(as_of, LIQUID_RATE)

    # Steps 2 & 3 -- mark to market, ratchet peaks
    for sym, h in portfolio.holdings.items():
        px = last.get(sym)
        if px is not None and not pd.isna(px):
            h.last_price = float(px)
        h.peak = max(h.peak, h.last_price)

    # Steps 4 & 5 -- score, rank, target top N
    scores = momentum_scores(closes)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top = [sym for sym, _ in ranked[:N_HOLD]]

    orders: list[Order] = []

    # Step 6 -- exits in priority order: hard SL -> trailing -> rotation
    for sym in list(portfolio.holdings):
        h = portfolio.holdings[sym]
        cp = h.last_price
        if cp <= h.entry_price * (1 - SL_PCT):
            reason = "SL"
        elif cp <= h.peak * (1 - TRAIL_PCT):
            reason = "Trail"
        elif sym not in top:
            reason = "Rotation"
        else:
            continue
        trade = portfolio.sell(sym, cp, as_of, reason)
        orders.append(Order("SELL", sym, trade["qty"], cp, reason, pnl_inr=trade["pnl_inr"]))

    # Step 7 -- entries: top-N not held, gated by 200-DMA; a failed gate
    # leaves the slot empty (no substitution with #N+1)
    allocation = portfolio.total_value() / N_HOLD
    skipped = []
    for sym in top:
        if sym in portfolio.holdings:
            continue
        if not above_dma(closes[sym]):
            skipped.append(sym)
            continue
        cp = float(last[sym])
        # Whole units; capped by available cash (drifted winners can leave < 1/N in cash)
        qty = min(math.floor(allocation / cp), math.floor(portfolio.cash / cp))
        if qty <= 0:
            continue
        portfolio.buy(sym, CATEGORY.get(sym, "Unknown"), qty, cp, as_of, scores[sym])
        orders.append(Order("BUY", sym, qty, cp, "ENTRY", score=scores[sym]))

    portfolio.last_rebalance = as_of.isoformat()
    return RebalanceResult(as_of, orders, top, skipped, interest, portfolio.total_value())
