"""Base class for strategy plug-ins."""
from __future__ import annotations

from context import Context, Leg


class Strategy:
    name = "base"

    def __init__(self, params: dict, lots: int, tag: str, lot_size: int):
        self.p = params
        self.lots = lots
        self.tag = tag
        self.qty = lots * lot_size

    def on_minute(self, ctx: Context) -> None:
        """Called once per closed 1-min candle (ctx.minute) on trading days."""
        raise NotImplementedError

    # ---- helpers shared by strategies --------------------------------
    def new_leg(self, expiry, strike: int, opt: str, sl_mult: float | None, disaster_mult: float | None, **meta) -> Leg:
        return Leg(strategy=self.name, tag=self.tag, expiry=expiry.isoformat(), strike=int(strike), opt=opt,
                   qty=self.qty, meta={"sl_mult": sl_mult, "disaster_mult": disaster_mult, **meta})

    @staticmethod
    def set_stops(leg: Leg) -> None:
        """Stops are set from the ACTUAL fill price, as in the backtest (SL = k x entry)."""
        m = leg.meta or {}
        if leg.entry_price is None:
            return
        if m.get("sl_mult"):
            leg.sl = round(leg.entry_price * m["sl_mult"], 2)
        if m.get("disaster_mult"):
            leg.disaster_sl = round(leg.entry_price * m["disaster_mult"], 2)
