"""
OTM4 1-day strangle  (backtest: NIFTY_DTE_EDGE_RESEARCH/code/p14_positional.py, OTM4_1day)

DTE1 (the session before the nearest weekly expiry):
  09:19 close  ATM = spot rounded to 50 -> CE strike ATM+200, PE strike ATM-200
  09:20 close  SELL each leg that has a valid price and traded recently (legs independent)
  every 1-min close, overnight included   close >= 1.5 x entry -> exit that leg
               (a stop seen on the 15:29 close is exited at the next session's open)
  expiry day 15:15 close   exit whatever is still open
"""
from __future__ import annotations

from datetime import date

from strategies.base import Strategy


class Otm4OneDay(Strategy):
    name = "otm4_1day"

    def on_minute(self, ctx) -> None:
        p = self.p
        m = ctx.minute
        st = ctx.sstate(self.name)
        today = ctx.today.isoformat()
        if st.get("day") != today:
            st.clear()
            st["day"] = today

        # ---- manage open legs (they live from DTE1 09:20 to expiry 15:15) ----
        for leg in ctx.open_legs(self.name):
            exp = date.fromisoformat(leg.expiry)
            if ctx.today >= exp and m >= p["exit_minute"]:
                ctx.close_leg(leg, "TIME_EXPIRY_1515")
                continue
            px = ctx.option(exp, leg.strike, leg.opt)
            if px is not None and leg.sl is not None and px >= leg.sl:
                ctx.close_leg(leg, "SL")

        expiry = ctx.nearest_expiry()
        if expiry is None or ctx.trading_sessions_until(expiry) != 1:
            return

        if m == p["select_minute"] and "atm" not in st:
            spot = ctx.spot()
            st["atm"] = None if spot is None else int(round(spot / 50) * 50)
            ctx.save()

        if m == p["entry_minute"] and st.get("atm") and not st.get("entered"):
            st["entered"] = True
            ctx.save()
            atm = st["atm"]
            legs = []
            for opt, k in (("CE", atm + p["offset"]), ("PE", atm - p["offset"])):
                px = ctx.option(expiry, k, opt)
                if px is None or px <= 0 or not ctx.liquid(expiry, k, opt):
                    ctx.log(f"[{self.name}] {opt} {k} has no valid price/volume - leg skipped")
                    continue
                legs.append(self.new_leg(expiry, k, opt, p["sl_mult"], p["disaster_mult"], ref_price=px))
            if legs and ctx.can_enter(self.name, self.lots):
                for leg in legs:
                    ctx.open_leg(leg, "ENTRY_DTE1_0920")
