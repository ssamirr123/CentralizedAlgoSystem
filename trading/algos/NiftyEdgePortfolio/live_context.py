"""
Live implementation of context.Context: Angel One prices + orders (or paper fills in DRY_RUN).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import config
import feed
import instruments
from broker import orders
from context import Context, Leg, minute_index
from risk import guard
from state import journal, store
from strategies.base import Strategy


def _market_open_now() -> bool:
    now = datetime.now()
    return (9, 15) <= (now.hour, now.minute) and (now.hour, now.minute, now.second) <= (15, 29, 50)


class LiveContext(Context):
    def __init__(self, st: dict):
        self.st = st
        self.today = date.today()
        self.minute = -1
        self.block_new = False
        self.day_pnl = 0.0
        self._bars_cache = {}       # (minutes, minute) -> bars

    # ---- market data -------------------------------------------------
    def spot(self):
        return feed.get_ltp(config.INDEX_TOKEN, exchange=config.INDEX_EXCH)

    def _tok(self, expiry, strike, opt):
        r = instruments.lookup(expiry, strike, opt)
        return r

    def option(self, expiry, strike, opt):
        r = self._tok(expiry, strike, opt)
        if r is None:
            return None
        feed.subscribe([r[1]])
        v = feed.get_ltp(r[1])
        return float(v) if v is not None and v > 0 else None

    def chain(self, expiry, opt, strikes):
        toks = {}
        for k in strikes:
            r = self._tok(expiry, k, opt)
            if r:
                toks[r[1]] = int(k)
        snap = feed.snapshot(list(toks))
        feed.subscribe(list(toks))
        return {toks[t]: v for t, v in snap.items() if t in toks and v and v > 0}

    def liquid(self, expiry, strike, opt):
        return self.option(expiry, strike, opt) is not None

    def expiries(self):
        return instruments.expiries(self.today)

    def trading_sessions_until(self, expiry):
        return instruments.trading_sessions_until(self.today, expiry)

    def previous_session(self):
        return instruments.previous_trading_days(self.today, 1)[-1]

    def _candles(self, frm: datetime, to: datetime, minutes: int):
        interval = {1: "ONE_MINUTE", 5: "FIVE_MINUTE", 10: "TEN_MINUTE", 15: "FIFTEEN_MINUTE"}[minutes]
        r = orders._retry(lambda _: config.objconn.getCandleData({
            "exchange": config.INDEX_EXCH, "symboltoken": config.INDEX_TOKEN, "interval": interval,
            "fromdate": frm.strftime("%Y-%m-%d %H:%M"), "todate": to.strftime("%Y-%m-%d %H:%M")}), "getCandleData", retries=3)
        out = []
        for row in (r or {}).get("data") or []:
            ts = datetime.fromisoformat(str(row[0]))
            out.append((ts.replace(tzinfo=None), float(row[1]), float(row[2]), float(row[3]), float(row[4])))
        return out

    def spot_bars(self, minutes):
        key = (minutes, self.minute)
        if key not in self._bars_cache:
            self._bars_cache = {}
            d0 = datetime.combine(self.today, datetime.min.time())
            rows = self._candles(d0.replace(hour=9, minute=15), d0.replace(hour=15, minute=30), minutes)
            bars = []
            for ts, o, h, l, c in rows:
                s = minute_index(ts)
                end = min(s + minutes - 1, 374)
                if 0 <= s <= 374 and end <= self.minute:          # completed bars only
                    bars.append((s // minutes, o, h, l, c))
            self._bars_cache[key] = bars
        return self._bars_cache[key]

    def spot_history_bars(self, minutes, days):
        days = days or 20
        prev = instruments.previous_trading_days(self.today, days)
        out = []
        for d in prev:                     # one call per day keeps within the API's range limits
            d0 = datetime.combine(d, datetime.min.time())
            for ts, o, h, l, c in self._candles(d0.replace(hour=9, minute=15), d0.replace(hour=15, minute=30), minutes):
                s = minute_index(ts)
                if 0 <= s <= 374:
                    out.append((d.isoformat(), s // minutes, o, h, l, c))
        return out

    # ---- orders --------------------------------------------------------
    def _deferred_ids(self):
        return {d["id"] for d in self.st["deferred_exits"]}

    def open_leg(self, leg: Leg, reason: str):
        if not _market_open_now():
            print(f"[{leg.strategy}] {reason}: market closed - entry skipped")
            return None
        r = self._tok(leg.expiry, leg.strike, leg.opt)
        if r is None:
            print(f"[{leg.strategy}] no instrument for {leg.expiry} {leg.strike}{leg.opt} - entry skipped")
            return None
        leg.symbol, leg.token = r
        feed.subscribe([leg.token])
        q, avg = orders.execute(leg.symbol, leg.token, leg.qty, "SELL", leg.tag)
        if not q or avg is None:
            print(f"[{leg.strategy}] ENTRY FAILED {leg.symbol} ({reason})")
            return None
        leg.qty = int(q)
        leg.entry_price = float(avg)
        leg.entry_dt = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        Strategy.set_stops(leg)
        if config.PLACE_BROKER_SL and leg.disaster_sl:
            leg.broker_sl_oid = orders.place_stoploss(leg.symbol, leg.token, leg.qty, leg.disaster_sl, leg.tag)
        store.put_leg(self.st, leg)
        print(f"[{leg.strategy}] ENTRY {reason} SELL {leg.symbol} qty={leg.qty} @ {leg.entry_price} "
              f"SL(close)={leg.sl} disaster={leg.disaster_sl} ref={leg.meta.get('ref_price')}")
        return leg

    def close_leg(self, leg: Leg, reason: str) -> bool:
        if leg.status != "OPEN" or leg.id in self._deferred_ids():
            return False
        if not _market_open_now():
            self.st["deferred_exits"].append({"id": leg.id, "reason": reason})
            store.save(self.st)
            print(f"[{leg.strategy}] {reason} on {leg.symbol} after the close - will exit at the next open")
            return True
        # a resting broker SL may already have filled
        if leg.broker_sl_oid and not config.DRY_RUN:
            status, avg = orders.sl_status(leg.broker_sl_oid)
            if status == "complete":
                self.record_broker_sl_fill(leg, avg)
                return True
            orders.cancel(leg.broker_sl_oid, "STOPLOSS")
        q, avg = orders.execute(leg.symbol, leg.token, leg.qty, "BUY", leg.tag, emergency=reason.startswith("EMERGENCY"))
        if not q:
            print(f"[{leg.strategy}] EXIT FAILED {leg.symbol} ({reason}) - STILL OPEN, CHECK THE BROKER")
            return False
        self._finish(leg, avg, reason)
        return True

    def record_broker_sl_fill(self, leg: Leg, avg):
        self._finish(leg, avg, "BROKER_SL")

    def _finish(self, leg, price, reason):
        leg.status = "CLOSED"
        leg.exit_price = None if price is None else float(price)
        leg.exit_dt = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        leg.exit_reason = reason
        net = journal.record(self.st, leg)
        store.put_leg(self.st, leg)
        print(f"[{leg.strategy}] EXIT {reason} BUY {leg.symbol} qty={leg.qty} @ {leg.exit_price} "
              f"(entry {leg.entry_price}) net Rs {net:,.0f}")

    def run_deferred_exits(self):
        pend, self.st["deferred_exits"] = self.st["deferred_exits"], []
        store.save(self.st)
        open_by_id = {l.id: l for l in store.legs(self.st)}
        for d in pend:
            leg = open_by_id.get(d["id"])
            if leg:
                self.close_leg(leg, d["reason"] + "_NEXT_OPEN")

    def refresh_broker_sls(self):
        """Resting SLs are DAY orders: re-place them each morning for overnight legs."""
        for leg in store.legs(self.st):
            if config.PLACE_BROKER_SL and leg.disaster_sl:
                status, _ = orders.sl_status(leg.broker_sl_oid)
                if status in ("open", "trigger pending", "pending", "modified"):
                    continue                     # still resting (restart during the day)
                leg.broker_sl_oid = orders.place_stoploss(leg.symbol, leg.token, leg.qty, leg.disaster_sl, leg.tag)
                store.put_leg(self.st, leg)

    def open_legs(self, strategy):
        dids = self._deferred_ids()
        return [l for l in store.legs(self.st) if l.strategy == strategy and l.id not in dids]

    # ---- state / risk / logging ------------------------------------------
    def sstate(self, strategy):
        return self.st["sstates"].setdefault(strategy, {})

    def save(self):
        store.save(self.st)

    def can_enter(self, strategy, lots):
        return guard.can_enter(self, strategy, lots)

    def log(self, msg):
        print(msg)
