"""
Live LTP feed (Angel One SmartWebSocketV2), adapted from DoubleStraddelAlgo.

- Keeps a set of subscribed tokens and re-subscribes ALL of them after every
  reconnect (each reconnect builds a fresh socket with an empty subscription list).
- get_ltp() falls back to REST; snapshot() batches REST quotes 50 at a time.
"""
from __future__ import annotations

import threading
import time

import config

_subs: dict[str, int] = {}        # token -> exchangeType (1 = NSE cash/index, 2 = NFO)
_lock = threading.Lock()
_last_tick = 0.0

# ---- "is the market actually trading today?" (calendar-independent holiday guard)
# On an exchange holiday the index price never changes. Orders sent anyway are
# queued by Angel One as AMOs and execute at the next open - so nothing may be
# sent until the NIFTY index has changed price a few times today.
MIN_INDEX_CHANGES = 3
_idx = {"day": None, "last": None, "changes": 0, "rest": [], "live": False}


def _idx_today():
    from datetime import date
    d = date.today().isoformat()
    if _idx["day"] != d:
        _idx.update(day=d, last=None, changes=0, rest=[], live=False)


def _note_index_price(px: float):
    _idx_today()
    if _idx["last"] is not None and px != _idx["last"]:
        _idx["changes"] += 1
    _idx["last"] = px


def market_live() -> bool:
    """True once today's NIFTY index price has really moved (websocket ticks, or two
    different REST quotes taken a minute apart if the websocket is down)."""
    _idx_today()
    if _idx["live"]:
        return True
    if _idx["changes"] >= MIN_INDEX_CHANGES:
        _idx["live"] = True
        return True
    if config.objconn is not None:
        try:
            v = float(config.objconn.ltpData(config.INDEX_EXCH, "", config.INDEX_TOKEN)["data"]["ltp"])
            _idx["rest"].append(v)
            if len(set(_idx["rest"])) >= 2:
                _idx["live"] = True
        except Exception as e:
            print(f"[WS] REST index quote failed: {e}")
    return _idx["live"]


# ---- 1-min NIFTY bars built from WebSocket ticks (no API calls; getCandleData only backfills gaps) ----
_bars = {"day": None, "m": {}, "partial": set(), "last_ts": None}


def _note_index_bar(px: float, now=None):
    from datetime import datetime
    now = now or datetime.now()
    m = now.hour * 60 + now.minute - 555
    if not 0 <= m <= 374:
        return
    d = now.date()
    if _bars["day"] != d:
        _bars.update(day=d, m={}, partial=set(), last_ts=None)
    last = _bars["last_ts"]
    if last is None or (now - last).total_seconds() > 60:
        _bars["partial"].add(m)          # first tick after a start / outage: this minute may be incomplete
    _bars["last_ts"] = now
    b = _bars["m"].get(m)
    if b is None:
        _bars["m"][m] = [px, px, px, px]
    else:
        if px > b[1]:
            b[1] = px
        if px < b[2]:
            b[2] = px
        b[3] = px


def index_bars(day=None):
    """{minute: (o, h, l, c)} of tick-built bars for today, and the set of minutes that may be incomplete."""
    from datetime import date
    if _bars["day"] != (day or date.today()):
        return {}, set()
    return {k: tuple(v) for k, v in list(_bars["m"].items())}, set(_bars["partial"])


def _on_data(wsapp, message):
    global _last_tick
    try:
        tok = str(message["token"])
        px = message["last_traded_price"] / 100.0
        config.ltp[tok] = px
        _last_tick = time.time()
        if tok == config.INDEX_TOKEN:
            _note_index_price(px)
            _note_index_bar(px)
    except Exception as e:
        print(f"[WS ERROR] on_data: {e}")


def _on_open(wsapp):
    print("[WS] socket open - resubscribing", len(_subs) + 1, "tokens")
    _send_subscribe({config.INDEX_TOKEN: 1, **_subs})


def _send_subscribe(tokmap: dict[str, int]):
    if config.sws is None or not tokmap:
        return
    groups = {}
    for t, ex in tokmap.items():
        groups.setdefault(ex, []).append(str(t))
    try:
        config.sws.subscribe("nep", 1, [{"exchangeType": ex, "tokens": toks} for ex, toks in groups.items()])
    except Exception as e:
        print(f"[WS] subscribe failed: {e}")


def connect():
    """Reconnect forever (run on a daemon thread)."""
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    while not config.kill_switch:
        try:
            config.sws = SmartWebSocketV2(config.token, config.apikey, config.clientid, config.objconn.getfeedToken())
            config.sws.on_open = _on_open
            config.sws.on_data = _on_data
            config.sws.on_error = lambda w, e: print(f"[WS ERROR] {e}")
            config.sws.on_close = lambda w: print("[WS] closed - will reconnect")
            config.sws.connect()
        except Exception as e:
            print(f"[WS] connection error: {e}")
        time.sleep(5)


def subscribe(tokens, exchange_type: int = 2):
    new = {}
    with _lock:
        for t in tokens:
            t = str(t)
            if t not in _subs:
                _subs[t] = exchange_type
                new[t] = exchange_type
    if new:
        _send_subscribe(new)


def feed_age() -> float:
    return time.time() - _last_tick if _last_tick else 1e9


def get_ltp(token, exchange=None, fallback_rest=True):
    t = str(token)
    if t in config.ltp:
        return config.ltp[t]
    if fallback_rest and config.objconn is not None:
        try:
            return float(config.objconn.ltpData(exchange or config.OPT_EXCH, "", t)["data"]["ltp"])
        except Exception as e:
            print(f"[WS] REST LTP failed for {t}: {e}")
    return None


def snapshot(tokens) -> dict[str, float]:
    """LTP for every token: websocket cache first, then batched REST quotes."""
    out, missing = {}, []
    for t in tokens:
        t = str(t)
        v = config.ltp.get(t)
        if v is not None:
            out[t] = v
        else:
            missing.append(t)
    for i in range(0, len(missing), 50):
        chunk = missing[i:i + 50]
        try:
            r = config.objconn.getMarketData("LTP", {config.OPT_EXCH: chunk})
            for q in (r or {}).get("data", {}).get("fetched", []) or []:
                out[str(q["symbolToken"])] = float(q["ltp"])
        except Exception as e:
            print(f"[WS] REST batch quote failed: {e}")
        time.sleep(0.35)
    return out
