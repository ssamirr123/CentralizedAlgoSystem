"""
The interface every strategy talks to. Strategies never call the broker, the
websocket or the clock directly - only a Context. Two implementations:

  * live.LiveContext      - Angel One prices + real/paper orders
  * replay.ReplayContext  - your historical 1-min CSVs, for checking that the
                            live code reproduces the backtest trade-for-trade

Price convention: ctx.minute is the candle that has just CLOSED (0 = 09:15).
spot()/option() return that candle's close (live: LTP ~1 s after it closed).
"""
from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import date, datetime


def minute_index(dt: datetime) -> int:
    """09:15 -> 0 ... 15:29 -> 374."""
    return dt.hour * 60 + dt.minute - 555


def hhmm(m: int) -> str:
    return f"{(m + 555) // 60:02d}:{(m + 555) % 60:02d}"


def charges(price: float, qty: int, side: str, d: date) -> float:
    """Exchange + statutory charges in Rs for one option order (same model as the backtests)."""
    ds = d.isoformat()
    prem = price * qty
    new = ds >= "2024-10-01"
    ex = prem * (0.0003503 if new else 0.000495)
    sebi = prem * 1e-6
    stt_rate = 0.0015 if ds >= "2026-04-01" else (0.001 if new else 0.000625)
    stt = prem * stt_rate if side == "SELL" else 0.0
    stamp = prem * 3e-5 if side == "BUY" else 0.0
    return 20 + ex + sebi + stt + stamp + 0.18 * (20 + ex + sebi)


_ids = itertools.count(1)


@dataclass
class Leg:
    strategy: str
    tag: str
    expiry: str            # YYYY-MM-DD
    strike: int
    opt: str               # CE | PE
    qty: int
    side: str = "SELL"
    symbol: str = ""
    token: str = ""
    entry_price: float | None = None
    entry_dt: str = ""
    sl: float | None = None           # strategy stop (checked on 1-min closes)
    disaster_sl: float | None = None  # intrabar safety stop (live only)
    broker_sl_oid: str | None = None
    status: str = "OPEN"              # OPEN | CLOSED
    exit_price: float | None = None
    exit_dt: str = ""
    exit_reason: str = ""
    meta: dict = field(default_factory=dict)
    id: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = f"{self.tag}-{datetime.now():%Y%m%d%H%M%S}-{next(_ids)}"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Leg":
        return Leg(**d)

    def pnl_points(self, price: float) -> float:
        return (self.entry_price - price) if self.side == "SELL" else (price - self.entry_price)


class Context(ABC):
    """What a strategy may ask for and do."""

    today: date
    minute: int

    # ---- market data -------------------------------------------------
    @abstractmethod
    def spot(self) -> float | None: ...

    @abstractmethod
    def option(self, expiry: date, strike: int, opt: str) -> float | None: ...

    @abstractmethod
    def chain(self, expiry: date, opt: str, strikes: list[int]) -> dict[int, float]:
        """{strike: close} for the strikes that have a valid price this minute."""

    @abstractmethod
    def expiries(self) -> list[date]:
        """Listed NIFTY weekly/monthly expiries on or after today, ascending."""

    @abstractmethod
    def trading_sessions_until(self, expiry: date) -> int:
        """0 on expiry day, 1 on the previous trading session, ..."""

    @abstractmethod
    def spot_bars(self, minutes: int) -> list[tuple]:
        """Today's COMPLETED spot bars of `minutes` length, aligned to 09:15:
        [(j, open, high, low, close), ...]. The last bar of the day is 15:25-15:29."""

    @abstractmethod
    def spot_history_bars(self, minutes: int, days: int) -> list[tuple]:
        """Completed bars of the previous `days` sessions (before today), oldest first:
        [(date_iso, j, open, high, low, close), ...]. Used to warm up indicators."""

    def previous_session(self) -> date | None:
        """The trading session before today (None if unknown)."""
        return None

    def entry_expiry(self) -> date | None:
        """Nearest weekly, or the NEXT weekly when today is the expiry day."""
        ex = self.expiries()
        if not ex:
            return None
        if ex[0] == self.today and len(ex) > 1:
            return ex[1]
        return ex[0]

    def liquid(self, expiry: date, strike: int, opt: str) -> bool:
        """Did the contract trade recently? Live: has an LTP. Replay: volume in the last 6 minutes."""
        return True

    def nearest_expiry(self) -> date | None:
        ex = self.expiries()
        return ex[0] if ex else None

    # ---- orders --------------------------------------------------------
    @abstractmethod
    def open_leg(self, leg: Leg, reason: str) -> Leg | None:
        """Send the entry order. Returns the leg with entry_price/dt filled in, or None."""

    @abstractmethod
    def close_leg(self, leg: Leg, reason: str) -> bool:
        """Send the exit order and journal the trade."""

    @abstractmethod
    def open_legs(self, strategy: str) -> list[Leg]: ...

    # ---- state / risk / logging ------------------------------------------
    @abstractmethod
    def sstate(self, strategy: str) -> dict:
        """Persistent per-strategy scratch dict (saved with the portfolio state)."""

    @abstractmethod
    def save(self) -> None: ...

    @abstractmethod
    def can_enter(self, strategy: str, lots: int) -> bool:
        """Risk gate: daily loss block, halt flag, margin."""

    @abstractmethod
    def log(self, msg: str) -> None: ...
