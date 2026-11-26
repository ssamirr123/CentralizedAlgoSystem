"""
BTST 9  (backtest: NIFTY_DTE_EDGE_RESEARCH/code/p12_investoruday.py, BTST9)

Every session (contract = nearest weekly; on expiry day the NEXT weekly):
  09:20 close  per side (CE and PE separately): the strike whose premium is closest
               to Rs100 becomes the reference; ref = its 09:20 close
  09:21-15:28  the first 1-min close <= 50% of ref arms the side; SELL it on the
               NEXT candle's close (one trade per side per day)
  every 1-min close, overnight included   close >= 1.5 x entry -> exit
  next session 09:20 close   exit whatever is still open
"""
from __future__ import annotations

from datetime import date

from strategies.base import Strategy


class Btst9(Strategy):
    name = "btst9"

    def on_minute(self, ctx) -> None:
        p = self.p
        m = ctx.minute
        st = ctx.sstate(self.name)
        today = ctx.today.isoformat()
        if st.get("day") != today:
            st.clear()
            st["day"] = today
            st["sides"] = {}

        # ---- manage open legs (entered on a previous session or earlier today) ----
        for leg in ctx.open_legs(self.name):
            entered_today = leg.entry_dt[:10] == today
            if not entered_today and m >= p["exit_minute"]:
                ctx.close_leg(leg, "TIME_0920")
                continue
            px = ctx.option(date.fromisoformat(leg.expiry), leg.strike, leg.opt)
            if px is not None and leg.sl is not None and px >= leg.sl:
                ctx.close_leg(leg, "SL")

        expiry = ctx.entry_expiry()
        if expiry is None:
            return
        sides = st["sides"]

        # ---- 09:20 close: choose the Rs100 strike per side --------------------
        if m == p["ref_minute"] and not sides:
            spot = ctx.spot()
            if spot is None:
                return
            atm = int(round(spot / 50) * 50)
            strikes = list(range(atm - 2000, atm + 2050, 50))
            for opt in ("CE", "PE"):
                chain = {k: v for k, v in ctx.chain(expiry, opt, strikes).items() if ctx.liquid(expiry, k, opt)}
                best, diff = None, None
                for k in sorted(chain):                    # first minimum wins (as np.argmin)
                    d = abs(chain[k] - p["target_premium"])
                    if diff is None or d < diff:
                        best, diff = k, d
                sides[opt] = None if best is None else {"strike": best, "ref": chain[best], "armed_at": None, "done": False}
            ctx.log(f"[{self.name}] reference strikes {sides}")
            ctx.save()
            return

        if not sides:
            return

        for opt, s in sides.items():
            if not s or s["done"]:
                continue
            # armed on the previous candle -> sell on this candle's close
            if s["armed_at"] is not None and m == s["armed_at"] + 1:
                s["done"] = True
                ctx.save()
                if ctx.can_enter(self.name, self.lots):
                    px = ctx.option(expiry, s["strike"], opt)
                    leg = self.new_leg(expiry, s["strike"], opt, p["sl_mult"], p["disaster_mult"], ref_price=px)
                    ctx.open_leg(leg, "ENTRY_FALL50")
                continue
            if s["armed_at"] is None and p["ref_minute"] < m <= p["last_trigger_minute"]:
                px = ctx.option(expiry, s["strike"], opt)
                if px is not None and px <= p["fall"] * s["ref"]:
                    s["armed_at"] = m
                    ctx.save()
