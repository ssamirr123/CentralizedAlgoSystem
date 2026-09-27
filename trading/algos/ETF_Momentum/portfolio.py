"""
Paper portfolio ledger, persisted to trading/data/ETF_Momentum.portfolio.json.

The strategy holds positions for months, so the ledger must survive process
restarts, server reboots and redeploys -- it is written atomically after
every change and reloaded on startup.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path


@dataclass
class Holding:
    symbol: str
    category: str
    qty: int
    entry_price: float
    entry_date: str            # ISO date
    peak: float                # ratchets at rebalances only (matches backtest)
    last_price: float          # latest mark, refreshed between rebalances too
    prev_close: float          # previous session close, for day P&L
    score_at_entry: float

    @property
    def cost(self) -> float:
        return self.qty * self.entry_price

    @property
    def value(self) -> float:
        return self.qty * self.last_price

    @property
    def unrealised_pnl(self) -> float:
        return self.value - self.cost


@dataclass
class Portfolio:
    initial_capital: float
    cash: float
    holdings: dict[str, Holding] = field(default_factory=dict)
    closed_trades: list[dict] = field(default_factory=list)
    last_rebalance: str | None = None      # ISO date of last completed rebalance
    last_accrual: str | None = None        # ISO date interest was last accrued to
    next_trade_id: int = 1

    @classmethod
    def new(cls, initial_capital: float) -> "Portfolio":
        return cls(initial_capital=initial_capital, cash=float(initial_capital))

    # ── valuation ────────────────────────────────────────────────────────────
    def holdings_value(self) -> float:
        return sum(h.value for h in self.holdings.values())

    def total_value(self) -> float:
        return self.cash + self.holdings_value()

    def day_pnl(self) -> float:
        return sum(h.qty * (h.last_price - h.prev_close) for h in self.holdings.values())

    # ── cash ─────────────────────────────────────────────────────────────────
    def accrue_interest(self, as_of: date, rate: float) -> float:
        """Simple interest on idle cash since the last accrual date."""
        interest = 0.0
        if self.last_accrual is not None:
            days = (as_of - date.fromisoformat(self.last_accrual)).days
            if days > 0:
                interest = self.cash * rate / 365 * days
                self.cash += interest
        self.last_accrual = as_of.isoformat()
        return interest

    # ── trades ───────────────────────────────────────────────────────────────
    def buy(self, symbol: str, category: str, qty: int, price: float, as_of: date, score: float) -> None:
        cost = qty * price
        if qty <= 0 or cost > self.cash + 1e-6:
            raise ValueError(f"cannot buy {qty} {symbol} @ {price}: cash {self.cash:.2f}")
        self.cash -= cost
        self.holdings[symbol] = Holding(
            symbol=symbol, category=category, qty=qty, entry_price=price,
            entry_date=as_of.isoformat(), peak=price, last_price=price, prev_close=price,
            score_at_entry=score,
        )

    def sell(self, symbol: str, price: float, as_of: date, reason: str) -> dict:
        h = self.holdings.pop(symbol)
        proceeds = h.qty * price
        self.cash += proceeds
        trade = {
            "trade_id": self.next_trade_id, "symbol": symbol, "category": h.category,
            "entry_date": h.entry_date, "exit_date": as_of.isoformat(),
            "entry_price": h.entry_price, "exit_price": price, "qty": h.qty,
            "cost": h.cost, "proceeds": proceeds, "pnl_inr": proceeds - h.cost,
            "pnl_pct": (price / h.entry_price - 1) * 100,
            "hold_days": (as_of - date.fromisoformat(h.entry_date)).days,
            "peak_price": h.peak, "exit_reason": reason,
            "momentum_score_at_entry": h.score_at_entry,
        }
        self.closed_trades.append(trade)
        self.next_trade_id += 1
        return trade

    # ── persistence ──────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d = asdict(self)
        d["holdings"] = {k: asdict(v) for k, v in self.holdings.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Portfolio":
        d = dict(d)
        d["holdings"] = {k: Holding(**v) for k, v in d.get("holdings", {}).items()}
        return cls(**d)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2))
        os.replace(tmp, path)

    @classmethod
    def load_or_new(cls, path: Path, initial_capital: float) -> "Portfolio":
        if path.exists():
            return cls.from_dict(json.loads(path.read_text()))
        return cls.new(initial_capital)
