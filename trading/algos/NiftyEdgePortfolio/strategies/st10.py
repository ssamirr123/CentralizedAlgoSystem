"""
ST10  (backtest: NIFTY_DTE_EDGE_RESEARCH/code/p12_investoruday.py run_st10 / P13 cfg H0)

10-min bars of NIFTY spot aligned to 09:15 (last bar 15:25-15:29), Supertrend(10, 3)
with Wilder ATR, computed continuously across days.
  direction flips on a bar close at minute cm:
      act on the close of minute cm+1 (next session 09:15 if the bar was the last one)
      close the current short, then SELL the ATM option against the trend:
      trend up -> ATM PE, trend down -> ATM CE; ATM = flip bar's close rounded to 50
      contract = nearest weekly, or the next weekly on expiry day
  expiry day 09:20 close: a short in the expiring weekly is rolled to the next weekly
      ATM (ATM from the 09:19 spot close), same side
  no stop-loss, no target (naked, as tested); held overnight.
"""
from __future__ import annotations

from datetime import date

from strategies.base import Strategy


class Supertrend:
    """Incremental Supertrend identical to the backtest's batch function."""

    def __init__(self, n: int = 10, mult: float = 3.0):
        self.n, self.mult = n, mult
        self.count = 0
        self.atr = self.fu = self.fl = self.prev_c = None
        self.dir = 1
        self._tr0 = []          # first n true ranges (for the initial ATR)
        self._pending = []      # first n bars kept until the initial ATR is known

    def to_dict(self):
        return dict(n=self.n, mult=self.mult, count=self.count, atr=self.atr, fu=self.fu, fl=self.fl,
                    prev_c=self.prev_c, dir=self.dir, _tr0=self._tr0, _pending=self._pending)

    @staticmethod
    def from_dict(d):
        s = Supertrend(d["n"], d["mult"])
        for k, v in d.items():
            setattr(s, k, v)
        return s

    def update(self, h: float, l: float, c: float) -> int:
        """Add one completed bar; returns the direction after it (1 up, -1 down)."""
        i = self.count
        tr = (h - l) if i == 0 else max(h - l, abs(h - self.prev_c), abs(l - self.prev_c))
        if i < self.n:
            # batch version: atr[:n] = mean(tr[:n]) -> must wait for n bars, then replay them
            self._tr0.append(tr)
            self._pending.append((h, l, c))
            self.count += 1
            self.prev_c = c
            if self.count == self.n:
                atr0 = sum(self._tr0) / self.n
                bars = self._pending
                self.count = 0
                self.prev_c = None
                for j, (hh, ll, cc) in enumerate(bars):
                    self._step(j, hh, ll, cc, atr0)
                self._pending = []
            return self.dir
        tr_atr = (self.atr * (self.n - 1) + tr) / self.n
        return self._step(i, h, l, c, tr_atr)

    def _step(self, i, h, l, c, atr):
        hl2 = (h + l) / 2
        ub, lb = hl2 + self.mult * atr, hl2 - self.mult * atr
        if i == 0:
            fu, fl, d = ub, lb, 1
        else:
            fu = ub if (ub < self.fu or self.prev_c > self.fu) else self.fu
            fl = lb if (lb > self.fl or self.prev_c < self.fl) else self.fl
            if self.dir == -1 and c > self.fu:
                d = 1
            elif self.dir == 1 and c < self.fl:
                d = -1
            else:
                d = self.dir
        self.atr, self.fu, self.fl, self.dir, self.prev_c = atr, fu, fl, d, c
        self.count = i + 1
        return d


class St10(Strategy):
    name = "st10"

    def on_minute(self, ctx) -> None:
        p = self.p
        m = ctx.minute
        st = ctx.sstate(self.name)
        n = p["bar_minutes"]

        # ---- warm-up (fresh start): indicator from history, no position until a flip ----
        if "st" not in st:
            s = Supertrend(p["atr_period"], p["multiplier"])
            for row in ctx.spot_history_bars(n, p["history_days"]):
                s.update(row[3], row[4], row[5])
            st["st"] = s.to_dict()
            st["last_bar"] = None
            st["bar_day"] = None
            st["pending"] = None
            st["prev_dir"] = s.dir
            if p.get("enter_on_start") and not ctx.open_legs(self.name):
                st["pending"] = {"side": s.dir, "close": None, "day": ctx.today.isoformat(), "minute": m}
            ctx.save()

        today = ctx.today.isoformat()
        if st.get("bar_day") != today:
            # New session. If the algo did not process every bar of the previous
            # session (down for a day, crashed mid-day), the saved indicator is stale:
            # rebuild it from history and re-align the position with the trend.
            prev = ctx.previous_session()
            last_n = (374 // n)
            complete = st.get("bar_day") is None or (prev is not None and st.get("bar_day") == prev.isoformat() and st.get("last_bar") == last_n)
            if not complete and st.get("bar_day") is not None:
                s = Supertrend(p["atr_period"], p["multiplier"])
                for row in ctx.spot_history_bars(n, p["history_days"]):
                    s.update(row[3], row[4], row[5])
                ctx.log(f"[{self.name}] indicator re-warmed (last processed {st.get('bar_day')} bar {st.get('last_bar')}); trend {s.dir}")
                st["st"] = s.to_dict()
                st["prev_dir"] = s.dir
                held = ctx.open_legs(self.name)
                held_side = (1 if held[0].opt == "PE" else -1) if held else None
                if held_side is not None and held_side != s.dir:
                    st["pending"] = {"side": s.dir, "close": None, "day": today, "minute": m}
            st["bar_day"] = today
            st["last_bar"] = None

        # ---- 1) a flip decided on an earlier bar: act on this candle's close ----
        pend = st.get("pending")
        if pend and (pend["day"] != today or m >= pend["minute"]):
            st["pending"] = None
            self._flip(ctx, pend["side"], pend["close"])
            ctx.save()

        # ---- 2) expiry-day roll at 09:20 ---------------------------------------
        if m == p["roll_minute"] - 1:
            st["spot_0919"] = ctx.spot()
        if m == p["roll_minute"]:
            for leg in ctx.open_legs(self.name):
                if leg.expiry == today:
                    side = 1 if leg.opt == "PE" else -1
                    ctx.close_leg(leg, "ROLL")
                    self._open(ctx, side, st.get("spot_0919"))
            ctx.save()

        # ---- 3) completed bars -> update the indicator, schedule a flip -------------
        # Processes every completed bar not yet seen (catches up if the live candle
        # feed was a few seconds late), in order.
        last = st.get("last_bar")
        if (m + 1) % n == 0 or m == 374 or last is None or last < (m + 1) // n - 1:
            for b in ctx.spot_bars(n):
                j = b[0]
                if last is not None and j <= last:
                    continue
                s = Supertrend.from_dict(st["st"])
                d = s.update(b[2], b[3], b[4])
                st["st"] = s.to_dict()
                st["last_bar"] = last = j
                if d != st.get("prev_dir"):
                    nxt = max(min(j * n + n - 1, 374) + 1, m + 1 if m > min(j * n + n - 1, 374) else 0)
                    st["pending"] = ({"side": d, "close": b[4], "day": today, "minute": nxt} if nxt <= 374
                                     else {"side": d, "close": b[4], "day": "NEXT", "minute": 0})
                st["prev_dir"] = d
                ctx.save()

    def _flip(self, ctx, side, bar_close):
        for leg in ctx.open_legs(self.name):
            ctx.close_leg(leg, "FLIP")
        self._open(ctx, side, bar_close if bar_close is not None else ctx.spot())

    def _open(self, ctx, side, ref_spot):
        if ref_spot is None or not ctx.can_enter(self.name, self.lots):
            return
        expiry = ctx.entry_expiry()
        k = int(round(ref_spot / 50) * 50)
        opt = "PE" if side > 0 else "CE"
        leg = self.new_leg(expiry, k, opt, None, self.p.get("disaster_mult"), trend=side)
        ctx.open_leg(leg, "ENTRY_FLIP")
