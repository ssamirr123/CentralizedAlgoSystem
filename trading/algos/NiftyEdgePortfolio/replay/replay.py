"""
Replay the LIVE strategy code over your historical 1-min NIFTY data and record
every trade, so it can be compared with the backtest trade lists.

Data layout (same as the research code):
  <DATA>/2021to2026_Nifty_Spot.csv            Date(YYYY-MM-DD),Time,Open,High,Low,Close
  <DATA>/Expiry.csv                           Dates
  <DATA>/<year>/<expiry>/<expiry>_<K><CE|PE>.csv   Date(DD/MM/YYYY),Time,O,H,L,Close,Volume,OI

Fill model (identical to the backtests):
  entries and time exits  -> close of the decision candle
  stop-loss exits          -> close of the NEXT candle (pessimistic)
No slippage or charges here: the comparison is on raw prices.

Usage:
  python -m replay.replay --data "/Users/samirkuila/Trading Strategy/Obsidian/data" --from 2025-01-01 --to 2026-10-06
"""
from __future__ import annotations

import argparse
import functools
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import config  # noqa: E402
from context import Context, Leg, hhmm  # noqa: E402
from strategies import REGISTRY  # noqa: E402


class ReplayData:
    def __init__(self, root: str):
        self.root = root
        sp = pd.read_csv(os.path.join(root, "2021to2026_Nifty_Spot.csv"))
        sp["m"] = sp.Time.str.slice(0, 2).astype(int) * 60 + sp.Time.str.slice(3, 5).astype(int) - 555
        sp = sp[(sp.m >= 0) & (sp.m < 375)].drop_duplicates(["Date", "m"], keep="last")
        self.ohlc = {}
        nbars = {}
        for d, g in sp.groupby("Date"):
            a = {}
            for col in ("Open", "High", "Low", "Close"):
                x = np.full(375, np.nan)
                x[g.m.values] = g[col].values
                a[col[0]] = pd.Series(x).ffill().bfill().values     # as the ST10 backtest
            self.ohlc[d] = a
            nbars[d] = len(g)
        self.all_dates = sorted(self.ohlc)                       # every session (for DTE counting)
        self.dates = [d for d in self.all_dates if nbars[d] >= 360]   # sessions the backtests traded
        self.expiries = sorted(pd.read_csv(os.path.join(root, "Expiry.csv")).Dates.astype(str).tolist())

    def bars(self, d: str, n: int):
        a = self.ohlc[d]
        out = []
        for j, s in enumerate(range(0, 375, n)):
            t = min(s + n - 1, 374)
            out.append((j, t, a["O"][s], a["H"][s:t + 1].max(), a["L"][s:t + 1].min(), a["C"][t]))
        return out

    @functools.lru_cache(maxsize=20000)
    def opt_days(self, expiry: str, strike: int, opt: str):
        f = os.path.join(self.root, expiry[:4], expiry, f"{expiry}_{strike}{opt}.csv")
        if not os.path.exists(f):
            return None
        df = pd.read_csv(f, usecols=["Date", "Time", "Close", "Volume"])
        df["d"] = pd.to_datetime(df.Date, format="%d/%m/%Y").dt.strftime("%Y-%m-%d")
        df["m"] = df.Time.str.slice(0, 2).astype(int) * 60 + df.Time.str.slice(3, 5).astype(int) - 555
        df = df[(df.m >= 0) & (df.m < 375)].drop_duplicates(["d", "m"], keep="last")
        out = {}
        for d, g in df.groupby("d"):
            c = np.full(375, np.nan)
            v = np.zeros(375)
            c[g.m.values] = g.Close.values
            v[g.m.values] = g.Volume.values
            out[d] = (pd.Series(c).ffill().values, v)   # close forward-filled within the day, like the panels
        return out


class ReplayContext(Context):
    def __init__(self, data: ReplayData):
        self.data = data
        self.today = None
        self.minute = 0
        self.legs: list[Leg] = []
        self.pending: list[tuple[Leg, str]] = []
        self.states: dict = {}
        self.trades: list[dict] = []
        self.logs: list[str] = []

    # ---- market data -------------------------------------------------
    def spot(self):
        v = self.data.ohlc[self.today.isoformat()]["C"][self.minute]
        return float(v) if np.isfinite(v) else None

    def spot_bars(self, minutes):
        return [(j, o, h, l, c) for j, t, o, h, l, c in self.data.bars(self.today.isoformat(), minutes) if t <= self.minute]

    def spot_history_bars(self, minutes, days):
        t = self.today.isoformat()
        prev = [d for d in self.data.dates if d < t]
        prev = prev if days is None else prev[-days:]
        return [(d, j, o, h, l, c) for d in prev for j, _t, o, h, l, c in self.data.bars(d, minutes)]

    def _arr(self, expiry, strike, opt, day=None):
        days = self.data.opt_days(expiry if isinstance(expiry, str) else expiry.isoformat(), int(strike), opt)
        if days is None:
            return None
        return days.get(day or self.today.isoformat())

    def option(self, expiry, strike, opt):
        a = self._arr(expiry, strike, opt)
        if a is None:
            return None
        v = a[0][self.minute]
        return float(v) if np.isfinite(v) and v > 0 else None

    def chain(self, expiry, opt, strikes):
        out = {}
        for k in strikes:
            v = self.option(expiry, k, opt)
            if v is not None:
                out[int(k)] = v
        return out

    def liquid(self, expiry, strike, opt):
        a = self._arr(expiry, strike, opt)
        if a is None:
            return False
        return a[1][max(0, self.minute - 5):self.minute + 1].sum() > 0

    def expiries(self):
        t = self.today.isoformat()
        return [date.fromisoformat(e) for e in self.data.expiries if e >= t]

    def previous_session(self):
        t = self.today.isoformat()
        prev = [d for d in self.data.dates if d < t]
        return date.fromisoformat(prev[-1]) if prev else None

    def trading_sessions_until(self, expiry):
        t, e = self.today.isoformat(), expiry.isoformat()
        return sum(1 for d in self.data.all_dates if t < d <= e)

    # ---- orders --------------------------------------------------------
    def open_leg(self, leg, reason):
        px = self.option(leg.expiry, leg.strike, leg.opt)
        if px is None:
            return None
        leg.entry_price = px
        leg.entry_dt = f"{self.today} {hhmm(self.minute)}"
        from strategies.base import Strategy
        Strategy.set_stops(leg)
        self.legs.append(leg)
        return leg

    def close_leg(self, leg, reason):
        if leg.status != "OPEN" or any(p[0] is leg for p in self.pending):
            return False
        if reason.startswith("SL"):
            self.pending.append((leg, reason))      # filled at the next candle's close
        else:
            self._fill_exit(leg, reason)
        return True

    def _fill_exit(self, leg, reason):
        a = self._arr(leg.expiry, leg.strike, leg.opt)
        px = float(a[0][self.minute]) if a is not None and np.isfinite(a[0][self.minute]) else None
        leg.status = "CLOSED"
        leg.exit_price = px
        leg.exit_dt = f"{self.today} {hhmm(self.minute)}"
        leg.exit_reason = reason
        self.trades.append(dict(strategy=leg.strategy, entry_date=leg.entry_dt[:10], entry_time=leg.entry_dt[11:], exit_date=str(self.today),
                                exit_time=hhmm(self.minute), expiry=leg.expiry, strike=leg.strike, leg=leg.opt,
                                entry=round(leg.entry_price, 2), exit=None if px is None else round(px, 2), why=reason))

    def open_legs(self, strategy):
        pend = {id(p[0]) for p in self.pending}
        return [l for l in self.legs if l.strategy == strategy and l.status == "OPEN" and id(l) not in pend]

    # ---- state / risk / logging ------------------------------------------
    def sstate(self, strategy):
        return self.states.setdefault(strategy, {})

    def save(self):
        pass

    def can_enter(self, strategy, lots):
        return True

    def log(self, msg):
        self.logs.append(f"{self.today} {hhmm(self.minute)} {msg}")

    # ---- driver ------------------------------------------------------------
    def run(self, strategies, start: str, end: str):
        for d in self.data.dates:
            if d < start or d > end:
                continue
            self.today = date.fromisoformat(d)
            for m in range(375):
                self.minute = m
                pend, self.pending = self.pending, []
                for leg, why in pend:
                    self._fill_exit(leg, why)
                for s in strategies:
                    s.on_minute(self)
        return pd.DataFrame(self.trades)


def build_strategies(names):
    out = []
    for n in names:
        cfg = config.STRATEGIES[n]
        params = getattr(config, n.upper())
        out.append(REGISTRY[n](params, lots=1, tag=cfg["tag"], lot_size=config.LOT_SIZE))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--from", dest="start", default="2021-01-01")
    ap.add_argument("--to", dest="end", default="2099-12-31")
    ap.add_argument("--strategies", default="strangle60")
    ap.add_argument("--out", default=os.path.join(HERE, "replay_trades.csv"))
    ap.add_argument("--full-history", action="store_true", help="warm indicators on ALL prior data (exact backtest match)")
    a = ap.parse_args()
    ctx = ReplayContext(ReplayData(a.data))
    strats = build_strategies(a.strategies.split(","))
    if a.full_history:
        for s in strats:
            if "history_days" in s.p:
                s.p = dict(s.p, history_days=None)
    T = ctx.run(strats, a.start, a.end)
    T.to_csv(a.out, index=False)
    print(f"{len(T)} trades -> {a.out}")


if __name__ == "__main__":
    main()
