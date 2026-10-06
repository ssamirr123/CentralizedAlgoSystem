"""
Paper ledger for EMA20_Pullback.

Intraday strategy — every leg opened in a session closes the same session.
JSON ledger survives restarts and resets daily counters on each new session.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional


@dataclass
class OpenLeg:
    direction: str
    strike: int
    expiry: str
    qty: int
    entry_price: float
    entry_dt: str
    spot_at_entry: float
    sl_price: float
    target1_price: float
    booked_half: bool = False
    booked_half_price: Optional[float] = None

    @property
    def entry_cost(self) -> float:
        return self.qty * self.entry_price


@dataclass
class Portfolio:
    initial_capital: float
    cash: float
    open_leg: Optional[OpenLeg] = None
    closed_trades: list[dict] = field(default_factory=list)
    session_date: Optional[str] = None
    losses_today: int = 0
    week_start_value: Optional[float] = None
    week_start_date: Optional[str] = None
    halted_until: Optional[str] = None
    next_trade_id: int = 1

    @classmethod
    def new(cls, initial_capital: float) -> "Portfolio":
        return cls(initial_capital=initial_capital, cash=float(initial_capital))

    def total_value(self, mark_price: Optional[float] = None) -> float:
        mtm = 0.0
        if self.open_leg is not None:
            mtm = self.open_leg.qty * (mark_price if mark_price is not None else self.open_leg.entry_price)
        return self.cash + mtm

    def day_pnl(self) -> float:
        iso = self.session_date
        if iso is None:
            return 0.0
        return sum(t["pnl_inr"] for t in self.closed_trades if t["exit_date"] == iso)

    def roll_session(self, today: date, equity: float) -> None:
        iso = today.isoformat()
        if self.session_date != iso:
            self.session_date = iso
            self.losses_today = 0
        iso_week = today.isocalendar()
        week_key = f"{iso_week.year}-W{iso_week.week:02d}"
        if self.week_start_date != week_key:
            self.week_start_date = week_key
            self.week_start_value = equity
            if self.halted_until == week_key:
                self.halted_until = None

    def register_loss(self) -> int:
        self.losses_today += 1
        return self.losses_today

    def weekly_dd_pct(self, equity: float) -> float:
        if not self.week_start_value or self.week_start_value <= 0:
            return 0.0
        return equity / self.week_start_value - 1.0

    def halt_this_week(self) -> None:
        self.halted_until = self.week_start_date

    def is_halted(self) -> bool:
        return self.halted_until == self.week_start_date

    def open(self, leg: OpenLeg) -> None:
        if self.open_leg is not None:
            raise ValueError("already have an open leg")
        cost = leg.entry_cost
        if cost > self.cash + 1e-6:
            raise ValueError(f"cannot open: cost {cost:.2f} > cash {self.cash:.2f}")
        self.cash -= cost
        self.open_leg = leg

    def book_half(self, price: float) -> float:
        leg = self.open_leg
        if leg is None or leg.booked_half:
            return 0.0
        half_qty = leg.qty // 2
        proceeds = half_qty * price
        self.cash += proceeds
        leg.qty -= half_qty
        leg.booked_half = True
        leg.booked_half_price = price
        return proceeds

    def close(self, exit_price: float, exit_dt: str, reason: str,
              brokerage_per_leg: float, cost_turnover_pct: float) -> dict:
        leg = self.open_leg
        if leg is None:
            raise ValueError("no open leg to close")
        proceeds = leg.qty * exit_price
        self.cash += proceeds

        if leg.booked_half and leg.booked_half_price is not None:
            avg_exit = 0.5 * leg.booked_half_price + 0.5 * exit_price
            full_qty_entry = leg.qty * 2
        else:
            avg_exit = exit_price
            full_qty_entry = leg.qty

        gross = (avg_exit - leg.entry_price) * full_qty_entry
        turnover = (leg.entry_price + avg_exit) * full_qty_entry
        costs = 2 * brokerage_per_leg + turnover * cost_turnover_pct
        pnl = gross - costs
        pnl_pct = (avg_exit / leg.entry_price - 1) * 100 if leg.entry_price > 0 else 0.0

        trade = {
            "trade_id": self.next_trade_id,
            "signal": "EMA20_Pullback",
            "direction": leg.direction,
            "strike": leg.strike,
            "expiry": leg.expiry,
            "entry_dt": leg.entry_dt,
            "exit_dt": exit_dt,
            "exit_date": exit_dt[:10],
            "entry_price": leg.entry_price,
            "exit_price": avg_exit,
            "qty": full_qty_entry,
            "booked_half": leg.booked_half,
            "booked_half_price": leg.booked_half_price,
            "spot_at_entry": leg.spot_at_entry,
            "pnl_inr": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "exit_reason": reason,
            "costs": round(costs, 2),
        }
        self.closed_trades.append(trade)
        self.next_trade_id += 1
        self.open_leg = None
        if pnl < 0:
            self.register_loss()
        return trade

    def to_dict(self) -> dict:
        d = asdict(self)
        d["open_leg"] = asdict(self.open_leg) if self.open_leg is not None else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Portfolio":
        d = dict(d)
        leg = d.get("open_leg")
        d["open_leg"] = OpenLeg(**leg) if leg is not None else None
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
