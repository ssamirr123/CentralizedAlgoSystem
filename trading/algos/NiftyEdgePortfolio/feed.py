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


def _on_data(wsapp, message):
    global _last_tick
    try:
        config.ltp[str(message["token"])] = message["last_traded_price"] / 100.0
        _last_tick = time.time()
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
