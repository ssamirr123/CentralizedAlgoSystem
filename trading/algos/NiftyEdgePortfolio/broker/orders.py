"""
Order execution, adapted from DoubleStraddelAlgo/broker/orders.py.

Differences:
  * producttype = config.PRODUCT_TYPE (CARRYFORWARD) - overnight legs must not be
    auto-squared-off by the broker
  * every order carries an ordertag (per strategy) so P&L and the manual-trade
    guard can tell algo orders apart
  * execute() waits for the fill and returns (filled_qty, average_price), walking
    the limit toward the market every PENDING_TIMEOUT seconds; exits fall back to
    a MARKET order for any unfilled remainder
  * DRY_RUN: nothing reaches the broker; fill = LTP -/+ PAPER_SLIPPAGE
"""
from __future__ import annotations

import threading
import time

import config
import feed

_API_MIN_INTERVAL = 0.35
_gate = threading.Lock()
_last = 0.0
_ob_lock = threading.Lock()
_ob_ts = 0.0
_pos_lock = threading.Lock()
_pos_ts = 0.0
_OPEN = ("open", "pending", "trigger pending", "modified", "open pending", "validation pending", "put order req received")


def _throttle():
    global _last
    with _gate:
        w = _API_MIN_INTERVAL - (time.monotonic() - _last)
        if w > 0:
            time.sleep(w)
        _last = time.monotonic()


def _retry(fn, label, retries=None, delay=None):
    retries = retries or config.ORDER_MAX_RETRIES
    delay = config.ORDER_RETRY_DELAY if delay is None else delay
    for attempt in range(1, retries + 1):
        try:
            _throttle()
            r = fn(attempt)
            if r is not None:
                return r
            back = delay
        except Exception as e:
            msg = str(e).lower()
            back = max(delay, 1.0) * (2 ** (attempt - 1)) if ("access rate" in msg or "access denied" in msg) else delay
            print(f"[ORDER RETRY] {label} {attempt}/{retries}: {e}")
        time.sleep(back)
    print(f"[ORDER FAILED] {label}: retries exhausted")
    return None


def _tick(p: float) -> float:
    return round(round(p * 20) / 20, 2)


def _limit(token, side, attempt):
    ltp = feed.get_ltp(token) or 0.0
    bump = config.LIMIT_SLIPPAGE * attempt
    return _tick(ltp + bump if side == "BUY" else max(0.05, ltp - bump))


def _params(symbol, token, qty, side, ordertype, price, tag, variety="NORMAL", trigger=None):
    p = {"variety": variety, "tradingsymbol": str(symbol), "symboltoken": str(token), "transactiontype": side,
         "exchange": config.OPT_EXCH, "ordertype": ordertype, "producttype": config.PRODUCT_TYPE, "duration": "DAY",
         "price": str(price), "squareoff": "0", "stoploss": "0", "quantity": str(int(qty)), "ordertag": tag[:20]}
    if trigger is not None:
        p["triggerprice"] = str(trigger)
    return p


def _place(params, label):
    def call(_):
        oid = config.objconn.placeOrder(params)
        if oid is None:
            raise ValueError("no order id (rejected?)")
        print(f"[ORDER] {params['ordertype']} {params['transactiontype']} {params['tradingsymbol']} qty={params['quantity']} "
              f"price={params['price']} tag={params['ordertag']} id={oid}")
        return oid
    return _retry(call, label)


def refresh_orderbook(force=False):
    global _ob_ts
    if config.DRY_RUN:
        return config.orderbook or []
    with _ob_lock:
        if not force and time.monotonic() - _ob_ts < 1.5:
            return config.orderbook
        r = _retry(lambda _: (config.objconn.orderBook() or {}).get("data") or [], "orderBook", retries=3)
        config.orderbook = r or []
        _ob_ts = time.monotonic()
        return config.orderbook


def refresh_positions(force=False):
    global _pos_ts
    if config.DRY_RUN:
        return config.positions or []
    with _pos_lock:
        if not force and time.monotonic() - _pos_ts < 1.5:
            return config.positions
        r = _retry(lambda _: (config.objconn.position() or {}).get("data") or [], "position", retries=3)
        config.positions = r or []
        _pos_ts = time.monotonic()
        return config.positions


def order(oid):
    for o in refresh_orderbook(force=True):
        if str(o.get("orderid")) == str(oid):
            return o
    return None


def _filled(o):
    try:
        q = int(float(o.get("filledshares") or 0))
        avg = float(o.get("averageprice") or 0) or None
    except Exception:
        q, avg = 0, None
    return q, avg


def cancel(oid, variety="NORMAL"):
    v = str(variety or "NORMAL").upper()
    v = "NORMAL" if v == "AMO" else v
    if config.DRY_RUN:
        print(f"[DRY RUN] CANCEL {oid}")
        return True
    return _retry(lambda _: config.objconn.cancelOrder(str(oid), v) or True, f"cancel({oid})")


def execute(symbol, token, qty, side, tag, emergency=False):
    """Place and see through one order. Returns (filled_qty, avg_price); (0, None) on failure."""
    side = side.upper()
    if config.DRY_RUN:
        ltp = feed.get_ltp(token)
        if ltp is None:
            print(f"[DRY RUN] {side} {symbol}: no LTP - not filled")
            return 0, None
        px = _tick(ltp + config.PAPER_SLIPPAGE if side == "BUY" else max(0.05, ltp - config.PAPER_SLIPPAGE))
        print(f"[DRY RUN] {side} {symbol} qty={qty} filled @{px} tag={tag}")
        return int(qty), px

    if emergency and config.ALLOW_MARKET_EMERGENCY:
        oid = _place(_params(symbol, token, qty, side, "MARKET", 0, tag), f"market({symbol})")
        return _await(oid, qty, symbol, token, side, tag, allow_modify=False) if oid else (0, None)

    oid = _place(_params(symbol, token, qty, side, "LIMIT", _limit(token, side, 1), tag), f"limit({symbol},{side})")
    if not oid:
        return 0, None
    q, avg = _await(oid, qty, symbol, token, side, tag, allow_modify=True)
    if q < int(qty) and tag and side == "BUY":
        # an EXIT must complete: market the remainder
        rem = int(qty) - q
        print(f"[ORDER] exit {symbol} short by {rem} - sending MARKET for the remainder")
        oid2 = _place(_params(symbol, token, rem, side, "MARKET", 0, tag), f"market-rem({symbol})")
        if oid2:
            q2, avg2 = _await(oid2, rem, symbol, token, side, tag, allow_modify=False)
            if q2:
                avg = ((avg or 0) * q + avg2 * q2) / (q + q2)
                q += q2
    return q, avg


def _await(oid, qty, symbol, token, side, tag, allow_modify):
    t0 = time.monotonic()
    attempt = 1
    last_mod = t0
    while time.monotonic() - t0 < config.FILL_TIMEOUT:
        time.sleep(1.0)
        o = order(oid)
        if not o:
            continue
        st = str(o.get("status", "")).lower()
        q, avg = _filled(o)
        if st == "complete":
            return q or int(qty), avg
        if st in ("rejected", "cancelled"):
            print(f"[ORDER] {symbol} {st}: {o.get('text', '')}")
            return q, avg
        if allow_modify and time.monotonic() - last_mod >= config.PENDING_TIMEOUT:
            attempt += 1
            last_mod = time.monotonic()
            p = _params(symbol, token, int(qty) - q, side, "LIMIT", _limit(token, side, attempt), tag)
            p["orderid"] = str(oid)
            p["quantity"] = str(int(qty))
            _retry(lambda _: config.objconn.modifyOrder(p) or True, f"modify({symbol})", retries=2)
    o = order(oid) or {}
    q, avg = _filled(o)
    if str(o.get("status", "")).lower() != "complete":
        cancel(oid, o.get("variety"))
    return q, avg


def place_stoploss(symbol, token, qty, trigger, tag):
    """Resting STOPLOSS_LIMIT BUY (protects a short if this process dies). Returns order id."""
    trigger = _tick(trigger)
    price = _tick(trigger + config.SL_LIMIT_BUFFER)
    if config.DRY_RUN:
        print(f"[DRY RUN] STOPLOSS BUY {symbol} qty={qty} trigger={trigger}")
        return f"DRYRUN-SL-{int(time.time() * 1000)}"
    return _place(_params(symbol, token, qty, "BUY", "STOPLOSS_LIMIT", price, tag, variety="STOPLOSS", trigger=trigger), f"sl({symbol})")


def sl_status(oid):
    """('complete'|'open'|..., avg_price) of a resting SL; ('', None) in DRY_RUN."""
    if config.DRY_RUN or not oid:
        return "", None
    o = order(oid)
    if not o:
        return "", None
    q, avg = _filled(o)
    return str(o.get("status", "")).lower(), avg


def has_pending(token) -> bool:
    for o in refresh_orderbook():
        if str(o.get("symboltoken")) == str(token) and str(o.get("status", "")).lower() in _OPEN and str(o.get("variety", "")).upper() != "STOPLOSS":
            return True
    return False


def available_margin():
    if config.DRY_RUN:
        return None
    try:
        r = config.objconn.rmsLimit()
        return float((r or {}).get("data", {}).get("net") or 0)
    except Exception as e:
        print(f"[ORDER] rmsLimit failed: {e}")
        return None
