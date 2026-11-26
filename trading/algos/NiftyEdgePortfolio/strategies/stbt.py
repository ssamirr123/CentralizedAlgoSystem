"""
STBT  "Sell Today Buy Tomorrow"  (backtest: NIFTY_DTE_EDGE_RESEARCH/code/p27_stbt.py, SELL reading)

Every session (contract = nearest weekly; on expiry day the NEXT weekly):
  range        high / low of NIFTY spot 1-min bars 09:15-11:14 (minutes 0..119)
  11:15-15:15  the first 1-min spot CLOSE above the range high = UP, below the range low = DOWN
               (one trade per day), acted on that candle's close:
                 UP   -> SELL PE  ATM+200   (200 points ITM)
                 DOWN -> SELL CE  ATM-200
               ATM = breakout close rounded to 50
  stop-loss    spot 0.5% of the breakout close AGAINST the trade, checked on every 1-min bar's
               high/low (same day, overnight gap and next morning) -> exit at that candle's close
  time exit    next session 09:35 close (minute 20)
Backtest 2021-01 -> 2026-11, 1 lot, after charges + 0.5 pt slippage: Rs8.22L, 1,225 trades, t=3.96, max DD -Rs56k.
"""
from __future__ import annotations

from datetime import date

from strategies.base import Strategy


class Stbt(Strategy):
    name = "stbt"

    def on_minute(self, ctx) -> None:
        p = self.p
        m = ctx.minute
        today = ctx.today.isoformat()
        st = ctx.sstate(self.name)
        if st.get("day") != today:
            st.clear()
            st["day"] = today
            st["done"] = False
            st["last_m"] = p["range_end_minute"] - 1

        legs = ctx.open_legs(self.name)
        bar = None
        if legs or (not st["done"] and p["range_end_minute"] <= m <= p["last_entry_minute"]):
            bars = {b[0]: b for b in ctx.spot_bars(1)}          # (j, o, h, l, c), j = minute index
            bar = bars.get(m)
        else:
            bars = {}

        # ---- manage open legs: spot SL (intrabar on the 1-min bar) and next-day 09:35 exit ----
        for leg in legs:
            meta = leg.meta or {}
            entered_today = leg.entry_dt[:10] == today
            if bar is not None and meta.get("spot_sl") is not None:
                _, _o, h, l, _c = bar
                if (meta["dir"] == "UP" and l <= meta["spot_sl"]) or (meta["dir"] == "DOWN" and h >= meta["spot_sl"]):
                    ctx.close_leg(leg, "SL_SPOT" if entered_today else ("SL_GAP" if m == 0 else "SL_SPOT_NEXTDAY"))
                    continue
            if not entered_today and m >= p["exit_minute"]:
                ctx.close_leg(leg, "TIME_0935")

        # ---- breakout scan ----
        if st["done"] or not (p["range_end_minute"] <= m <= p["last_entry_minute"]):
            return
        rng = [b for j, b in bars.items() if j < p["range_end_minute"]]
        if len(rng) < p["min_range_bars"]:
            ctx.log(f"[{self.name}] only {len(rng)} range bars - no trade today")
            st["done"] = True
            ctx.save()
            return
        hi = max(b[2] for b in rng)
        lo = min(b[3] for b in rng)
        signal = None
        for j in range(max(st["last_m"] + 1, p["range_end_minute"]), m + 1):   # catch up missed minutes
            b = bars.get(j)
            if b is None:
                continue
            if b[4] > hi:
                signal = ("UP", j, b[4]); break
            if b[4] < lo:
                signal = ("DOWN", j, b[4]); break
        st["last_m"] = m
        if signal is None:
            ctx.save()
            return
        st["done"] = True
        dirn, j, close = signal
        ctx.save()
        if m - j > p["max_late_minutes"]:
            ctx.log(f"[{self.name}] breakout {dirn} at minute {j} found late (now {m}) - skipped")
            return
        if ctx.open_legs(self.name):
            return
        expiry = ctx.entry_expiry()
        if expiry is None or not ctx.can_enter(self.name, self.lots):
            return
        atm = int(round(close / 50) * 50)
        opt, strike = ("PE", atm + p["itm_points"]) if dirn == "UP" else ("CE", atm - p["itm_points"])
        spot_sl = round(close * (1 - p["sl_pct"]) if dirn == "UP" else close * (1 + p["sl_pct"]), 2)
        ctx.log(f"[{self.name}] range {lo:.2f}-{hi:.2f} | breakout {dirn} close {close:.2f} -> SELL {strike}{opt}, spot SL {spot_sl}")
        leg = self.new_leg(expiry, strike, opt, None, p["disaster_mult"], dir=dirn, spot_in=close, spot_sl=spot_sl,
                           range_hi=hi, range_lo=lo)
        ctx.open_leg(leg, f"ENTRY_BREAKOUT_{dirn}")
