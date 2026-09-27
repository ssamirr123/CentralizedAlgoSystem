"""ETF_Momentum: rebalance rules (must match the backtest), ledger, scheduling."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from trading.algos.ETF_Momentum import strategy as strat_mod
from trading.algos.ETF_Momentum.config import EtfMomentumConfig, MIN_HISTORY, N_HOLD
from trading.algos.ETF_Momentum.main import IST, EtfMomentumRunner
from trading.algos.ETF_Momentum.portfolio import Portfolio
from trading.algos.ETF_Momentum.strategy import (
    above_dma, is_first_trading_day_of_month, momentum_scores, rebalance,
)
from trading.common.config import TradingConfig

SYMS = ["NIFTYBEES", "GOLDBEES", "SILVERBEES", "ITBEES", "BANKBEES",
        "PHARMABEES", "AUTOBEES", "CPSEETF", "MON100"]


def _closes(n=300, end="2026-09-01", drifts=None) -> pd.DataFrame:
    """Geometric price paths, one daily drift per symbol (strongest first)."""
    idx = pd.bdate_range(end=end, periods=n)
    drifts = drifts or {s: 0.0025 - i * 0.0004 for i, s in enumerate(SYMS)}
    return pd.DataFrame({s: 100 * np.exp(d * np.arange(n)) for s, d in drifts.items()}, index=idx)


def test_score_uses_positional_lookbacks():
    closes = _closes()
    s = closes["NIFTYBEES"]
    p0 = s.iloc[-1]
    expected = sum(w * (p0 / s.iloc[-(lb + 1)] - 1) * 100
                   for lb, w in zip((21, 63, 126, 252), (0.15, 0.40, 0.30, 0.15)))
    assert momentum_scores(closes)["NIFTYBEES"] == pytest.approx(expected)


def test_short_history_is_not_scored():
    closes = _closes()
    closes.loc[closes.index[: len(closes) - MIN_HISTORY + 1], "GOLDBEES"] = np.nan
    assert "GOLDBEES" not in momentum_scores(closes)


def test_first_trading_day_of_month():
    idx = pd.DatetimeIndex(["2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02"])
    assert is_first_trading_day_of_month(idx, date(2026, 9, 1))
    assert not is_first_trading_day_of_month(idx, date(2026, 9, 2))
    assert not is_first_trading_day_of_month(idx, date(2026, 9, 3))   # not a session


def test_fresh_rebalance_buys_top_n_in_whole_units():
    closes = _closes()
    as_of = closes.index[-1].date()
    p = Portfolio.new(1_000_000)
    result = rebalance(p, closes, as_of)

    assert sorted(p.holdings) == sorted(SYMS[:N_HOLD])
    assert all(o.side == "BUY" for o in result.orders)
    for h in p.holdings.values():
        assert isinstance(h.qty, int)
        assert h.qty == int((1_000_000 / N_HOLD) // h.entry_price)
        assert h.entry_price == pytest.approx(closes[h.symbol].iloc[-1])
    assert p.cash >= 0
    assert p.total_value() == pytest.approx(1_000_000)
    assert p.last_rebalance == as_of.isoformat()


def test_dma_failure_leaves_slot_empty(monkeypatch):
    closes = _closes()
    monkeypatch.setattr(strat_mod, "above_dma", lambda s: s.name != "GOLDBEES")
    p = Portfolio.new(1_000_000)
    result = rebalance(p, closes, closes.index[-1].date())
    assert "GOLDBEES" not in p.holdings
    assert SYMS[N_HOLD] not in p.holdings          # no substitution with #7
    assert len(p.holdings) == N_HOLD - 1
    assert result.skipped_below_dma == ["GOLDBEES"]


def test_above_dma():
    assert above_dma(pd.Series(np.linspace(100, 200, 250)))
    assert not above_dma(pd.Series(np.linspace(200, 100, 250)))


def _held(p: Portfolio, sym: str, entry: float, peak: float, as_of: date) -> None:
    p.buy(sym, "Sector", 10, entry, as_of, score=1.0)
    p.holdings[sym].peak = peak


def test_exit_priority_sl_then_trail_then_rotation():
    closes = _closes()
    as_of = closes.index[-1].date()
    last = closes.iloc[-1]
    p = Portfolio.new(1_000_000)
    entry_date = as_of - timedelta(days=60)
    # In the top 6 but down 20% from entry -> SL wins over everything
    _held(p, "NIFTYBEES", last["NIFTYBEES"] / 0.80, last["NIFTYBEES"] / 0.80, entry_date)
    # Up from entry but 25% off its peak -> Trail
    _held(p, "GOLDBEES", last["GOLDBEES"] * 0.9, last["GOLDBEES"] / 0.75, entry_date)
    # Healthy but ranked last -> Rotation
    _held(p, "MON100", last["MON100"] * 0.95, last["MON100"], entry_date)
    # Healthy and in the top 6 -> hold
    _held(p, "SILVERBEES", last["SILVERBEES"] * 0.95, last["SILVERBEES"], entry_date)

    result = rebalance(p, closes, as_of)
    reasons = {o.symbol: o.reason for o in result.orders if o.side == "SELL"}
    assert reasons == {"NIFTYBEES": "SL", "GOLDBEES": "Trail", "MON100": "Rotation"}
    assert "SILVERBEES" in p.holdings
    assert [t["trade_id"] for t in p.closed_trades] == [1, 2, 3]


def test_buy_capped_by_available_cash():
    closes = _closes()
    as_of = closes.index[-1].date()
    p = Portfolio.new(1_000_000)
    rebalance(p, closes, as_of)
    # Free one slot but leave far less than 1/N in cash: the re-entry is
    # sized to the cash actually available, never overdrawn.
    p.holdings.pop("NIFTYBEES")
    p.cash = 5_000.0
    result = rebalance(p, closes, as_of)
    (buy,) = [o for o in result.orders if o.side == "BUY"]
    assert buy.symbol == "NIFTYBEES"
    assert buy.qty * buy.price <= 5_000.0
    assert p.cash >= 0


def test_interest_accrues_on_idle_cash_between_rebalances():
    p = Portfolio.new(100_000)
    p.accrue_interest(date(2026, 1, 1), 0.05)
    assert p.cash == 100_000                      # first call just sets the anchor
    p.accrue_interest(date(2026, 1, 31), 0.05)
    assert p.cash == pytest.approx(100_000 * (1 + 0.05 / 365 * 30))


def test_ledger_roundtrip(tmp_path):
    closes = _closes()
    p = Portfolio.new(1_000_000)
    rebalance(p, closes, closes.index[-1].date())
    path = tmp_path / "ETF_Momentum.portfolio.json"
    p.save(path)
    q = Portfolio.load_or_new(path, 5)
    assert q.to_dict() == p.to_dict()


# ── scheduling ──────────────────────────────────────────────────────────────
def _runner(tmp_path, **overrides) -> EtfMomentumRunner:
    cfg = EtfMomentumConfig(state_file=tmp_path / "p.json", **overrides)
    return EtfMomentumRunner(TradingConfig(), cfg, "test", logging.getLogger("t"), None)


def _ist(y, m, d, hh=16, mm=5) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_rebalance_not_due_before_close_or_on_weekend(tmp_path):
    r = _runner(tmp_path)
    assert not r._rebalance_due(_ist(2026, 9, 1, 15, 0))     # before 16:00
    assert not r._rebalance_due(_ist(2026, 8, 1))            # Saturday
    assert r._rebalance_due(_ist(2026, 9, 1))


def test_fresh_start_waits_for_next_month(tmp_path):
    assert not _runner(tmp_path)._rebalance_due(_ist(2026, 9, 15))
    assert _runner(tmp_path, start_now=True)._rebalance_due(_ist(2026, 9, 15))


def test_not_due_again_in_same_month(tmp_path):
    r = _runner(tmp_path)
    r.portfolio.last_rebalance = "2026-09-01"
    assert not r._rebalance_due(_ist(2026, 9, 2))
    assert r._rebalance_due(_ist(2026, 10, 1))
    assert r._rebalance_due(_ist(2026, 10, 15))              # active + missed -> catch up
