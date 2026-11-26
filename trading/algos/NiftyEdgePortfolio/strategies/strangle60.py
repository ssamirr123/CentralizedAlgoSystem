"""
₹60 expiry-day strangle  (backtest: NIFTY_DTE_EDGE_RESEARCH/code/p10_strangle60_exit1410.py, exit_cfg 1515)

Expiry day only (nearest weekly expires today):
  09:19 close  ATM = spot rounded to 50. CE = strike >= ATM whose premium is
               nearest Rs60; PE = strike <= ATM nearest Rs60. If either leg's
               best premium is more than Rs18 away from 60, skip the day.
  09:20 close  SELL both legs (skip the day if either has no valid price/volume).
  each 1-min close   if close >= 2 x entry premium -> exit that leg (legs independent).
  15:15 close  exit whatever is still open.
"""
from __future__ import annotations

from strategies.base import Strategy


class Strangle60(Strategy):
    name = "strangle60"

    def on_minute(self, ctx) -> None:
        p = self.p
        m = ctx.minute
        st = ctx.sstate(self.name)
        today = ctx.today.isoformat()
        if st.get("day") != today:
            st.clear()
            st["day"] = today

        legs = ctx.open_legs(self.name)
        expiry = ctx.nearest_expiry()
        is_expiry = expiry is not None and expiry == ctx.today

        # ---- manage open legs first (also covers restarts) -----------------
        for leg in legs:
            px = ctx.option(_d(leg.expiry), leg.strike, leg.opt)
            if m >= p["exit_minute"]:
                ctx.close_leg(leg, "TIME_1515")
            elif px is not None and leg.sl is not None and px >= leg.sl:
                ctx.close_leg(leg, "SL")

        if not is_expiry:
            return

        # ---- 09:19 close: pick strikes ---------------------------------------
        if m == p["select_minute"] and "picked" not in st:
            st["picked"] = self._pick(ctx, expiry)
            ctx.log(f"[{self.name}] picked {st['picked']}")
            ctx.save()

        # ---- 09:20 close: enter ------------------------------------------------
        if m == p["entry_minute"] and st.get("picked") and not st.get("entered"):
            st["entered"] = True          # one attempt per day, even if skipped below
            ctx.save()
            picked = st["picked"]
            prices = {}
            for opt in ("CE", "PE"):
                k = picked[opt]
                px = ctx.option(expiry, k, opt)
                if px is None or px <= 0 or not ctx.liquid(expiry, k, opt):
                    ctx.log(f"[{self.name}] {opt} {k} has no valid price/volume at entry - skipping today")
                    return
                prices[opt] = px
            if not ctx.can_enter(self.name, self.lots):
                ctx.log(f"[{self.name}] risk gate refused entry")
                return
            for opt in ("CE", "PE"):
                leg = self.new_leg(expiry, picked[opt], opt, p["sl_mult"], p["disaster_mult"], ref_price=prices[opt])
                ctx.open_leg(leg, "ENTRY_0920")

    def _pick(self, ctx, expiry):
        p = self.p
        spot = ctx.spot()
        if spot is None:
            return None
        step = 50
        atm = int(round(spot / step) * step)
        width = 2000
        out = {}
        for opt in ("CE", "PE"):
            strikes = list(range(atm, atm + width + step, step)) if opt == "CE" else list(range(atm - width, atm + step, step))
            chain = ctx.chain(expiry, opt, strikes)
            best, best_diff = None, None
            for k in sorted(chain):            # ascending, first minimum wins (as np.argmin)
                diff = abs(chain[k] - p["target_premium"])
                if best_diff is None or diff < best_diff:
                    best, best_diff = k, diff
            if best is None or best_diff > p["max_premium_diff"]:
                return None
            out[opt] = best
        return out


def _d(s):
    from datetime import date
    return date.fromisoformat(s)
