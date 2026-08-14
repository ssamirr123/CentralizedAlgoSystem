"""
Order execution layer.

- All entry/exit orders are LIMIT orders (deliverable: limit order placement logic).
- Market orders are used ONLY by the emergency square-off path (config.ALLOW_MARKET_EMERGENCY).
- Every placement is wrapped in retry logic: max ORDER_MAX_RETRIES, ORDER_RETRY_DELAY apart,
  re-pricing the limit toward the market on each attempt (deliverable: retry mechanism).
- Pending orders beyond PENDING_TIMEOUT are modified / cancelled / market-converted per config.
- Partial fills are detected and only the filled/unfilled remainder is managed.
- Order-book / position reconciliation helpers keep cached state fresh.
"""
import config
import time
import threading
import websocket_feed as wf

# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
# Angel One SmartAPI throttles endpoints (order book ~1 req/sec). The double
# straddle places several legs, each spawning a _manage_pending thread that all
# wake together after PENDING_TIMEOUT and hammer orderBook() at once -> the
# broker replies "Access denied because of exceeding access rate".
#
# We defend on two fronts:
#   1. A global min-interval throttle so we never fire API calls back-to-back.
#   2. Coalesced, time-cached reads for orderBook/position so concurrent threads
#      share a single fresh fetch instead of each issuing its own request.

# Minimum gap enforced between ANY two broker API calls (seconds).
_API_MIN_INTERVAL = 0.35
_api_gate_lock = threading.Lock()
_last_api_ts = 0.0

# Read-cache freshness windows (seconds).
_ORDERBOOK_TTL = 1.5
_POSITION_TTL = 1.5


def _is_rate_limit_error(e):
    msg = str(e).lower()
    return 'exceeding access rate' in msg or 'access denied' in msg or 'rate' in msg


def _api_throttle():
    """Block until at least _API_MIN_INTERVAL has passed since the last call."""
    global _last_api_ts
    with _api_gate_lock:
        wait = _API_MIN_INTERVAL - (time.monotonic() - _last_api_ts)
        if wait > 0:
            time.sleep(wait)
        _last_api_ts = time.monotonic()


def _retry(fn, label, retries=None, delay=None):
    retries = retries or config.ORDER_MAX_RETRIES
    delay = delay if delay is not None else config.ORDER_RETRY_DELAY
    for attempt in range(1, retries + 1):
        try:
            _api_throttle()
            result = fn(attempt)
            if result is not None:
                return result
            backoff = delay
        except Exception as e:
            # Rate-limit errors need a much longer, growing cool-down; other
            # errors use the normal fixed delay.
            if _is_rate_limit_error(e):
                backoff = max(delay, 1.0) * (2 ** (attempt - 1))
                print(f'[RATE LIMIT] {label} attempt {attempt}/{retries} - '
                      f'backing off {backoff:.1f}s')
            else:
                backoff = delay
                print(f'[ORDER RETRY] {label} attempt {attempt}/{retries} error: {e}')
        time.sleep(backoff)
    print(f'[ORDER FAILED] {label} - {retries} retries exhausted')
    return None


def _round_tick(price):
    """Round to the 0.05 NFO tick."""
    return round(price * 20) / 20


def _limit_price(token, side, attempt):
    """
    Aggressive limit that walks toward the market each retry so rejected/unfilled
    orders get progressively more fillable (deliverable: retry with updated limit price).
    """
    ltp = wf.get_ltp(token) or 0.0
    bump = config.LIMIT_SLIPPAGE * attempt
    price = ltp + bump if side == 'BUY' else max(0.05, ltp - bump)
    return _round_tick(price)


def place_limit(symbol, token, qty, side):
    """Place a LIMIT order with retries, then manage it if it stays pending."""
    if getattr(config, 'DRY_RUN', False):
        price = _limit_price(token, side.upper(), 1)
        oid = f'DRYRUN-{int(time.time() * 1000)}'
        print(f'[DRY RUN] LIMIT {side} {symbol} qty={qty} price={price} id={oid}')
        return oid

    def _call(attempt):
        params = {
            "variety": "NORMAL",
            "tradingsymbol": str(symbol),
            "symboltoken": str(token),
            "transactiontype": side.upper(),
            "exchange": config.OPT_EXCH,
            "ordertype": "LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(_limit_price(token, side.upper(), attempt)),
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(qty),
        }
        oid = config.objconn.placeOrder(params)
        if oid is None:
            raise ValueError('no order id returned (possibly rejected)')
        print(f'[ORDER] LIMIT {side} {symbol} qty={qty} price={params["price"]} id={oid}')
        return oid

    oid = _retry(_call, f'place_limit({symbol},{side})')
    if oid:
        # Manage the pending order on a background thread so placement returns
        # immediately - this keeps the two straddle legs effectively simultaneous.
        threading.Thread(
            target=_manage_pending, args=(oid, symbol, token, qty, side), daemon=True
        ).start()
    return oid


def place_market(symbol, token, qty, side):
    """MARKET order - used ONLY by emergency square-off; otherwise degrades to LIMIT."""
    if getattr(config, 'DRY_RUN', False):
        oid = f'DRYRUN-{int(time.time() * 1000)}'
        print(f'[DRY RUN] MARKET(EMERGENCY) {side} {symbol} qty={qty} id={oid}')
        return oid

    if not config.ALLOW_MARKET_EMERGENCY:
        return place_limit(symbol, token, qty, side)

    def _call(attempt):
        params = {
            "variety": "NORMAL",
            "tradingsymbol": str(symbol),
            "symboltoken": str(token),
            "transactiontype": side.upper(),
            "exchange": config.OPT_EXCH,
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(qty),
        }
        oid = config.objconn.placeOrder(params)
        if oid is None:
            raise ValueError('no order id returned')
        print(f'[ORDER] MARKET(EMERGENCY) {side} {symbol} qty={qty} id={oid}')
        return oid

    return _retry(_call, f'place_market({symbol},{side})')


def modify_limit(orderid, symbol, token, qty, side, attempt=1):
    if getattr(config, 'DRY_RUN', False):
        price = _limit_price(token, side.upper(), attempt)
        print(f'[DRY RUN] MODIFY {symbol} id={orderid} newprice={price}')
        return True

    def _call(_):
        params = {
            "variety": "NORMAL",
            "orderid": str(orderid),
            "tradingsymbol": str(symbol),
            "symboltoken": str(token),
            "exchange": config.OPT_EXCH,
            "ordertype": "LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(_limit_price(token, side.upper(), attempt)),
            "quantity": str(qty),
        }
        r = config.objconn.modifyOrder(params)
        print(f'[ORDER] MODIFY {symbol} id={orderid} newprice={params["price"]}')
        return r or True

    return _retry(_call, f'modify_limit({symbol})')


def cancel(orderid):
    if getattr(config, 'DRY_RUN', False):
        print(f'[DRY RUN] CANCEL id={orderid}')
        return True

    def _call(_):
        r = config.objconn.cancelOrder(str(orderid), "NORMAL")
        print(f'[ORDER] CANCEL id={orderid}')
        return r or True

    return _retry(_call, f'cancel({orderid})')


def order_status(orderid):
    """Look up an order in the cached order book (deliverable: order status reconciliation)."""
    for o in (config.orderbook or []):
        if str(o.get('orderid')) == str(orderid):
            return o
    return None


def _manage_pending(orderid, symbol, token, qty, side):
    """
    After PENDING_TIMEOUT, act on a still-open LIMIT order per config.PENDING_ACTION.
    Detects partial fills and only manages the unfilled remainder.
    """
    time.sleep(config.PENDING_TIMEOUT)
    refresh_orderbook()
    o = order_status(orderid)
    if not o:
        return
    status = str(o.get('status', '')).lower()
    try:
        filled = int(float(o.get('filledshares', 0) or 0))
    except Exception:
        filled = 0
    if status == 'complete':
        return
    if 0 < filled < int(qty):
        print(f'[PARTIAL FILL] {symbol} filled={filled}/{qty} - managing remainder only')
    remainder = int(qty) - filled
    if remainder <= 0:
        return

    if config.PENDING_ACTION == 'CANCEL':
        cancel(orderid)
    elif config.PENDING_ACTION == 'MODIFY':
        modify_limit(orderid, symbol, token, remainder, side, attempt=2)
    elif config.PENDING_ACTION == 'MARKET' and config.ALLOW_MARKET_EMERGENCY:
        cancel(orderid)
        place_market(symbol, token, remainder, side)


# --------------------------------------------------------------------------- #
# Reconciliation helpers
# --------------------------------------------------------------------------- #
_orderbook_lock = threading.Lock()
_last_orderbook_ts = 0.0
_position_lock = threading.Lock()
_last_position_ts = 0.0


def refresh_orderbook(force=False):
    """
    Fetch the order book, coalescing concurrent callers.

    Multiple _manage_pending threads wake together and all want the order book;
    without coalescing that's a burst of orderBook() calls that trips the broker
    rate limit. The lock serialises them: the first thread fetches, the rest
    find the cache fresh (within _ORDERBOOK_TTL) and reuse it - one API call
    instead of N.
    """
    if getattr(config, 'DRY_RUN', False):
        # Paper trading: simulated orders never reach the broker, so don't
        # poll the real API - just return the locally cached book.
        return config.orderbook or []

    global _last_orderbook_ts
    with _orderbook_lock:
        age = time.monotonic() - _last_orderbook_ts
        if not force and config.orderbook is not None and age < _ORDERBOOK_TTL:
            return config.orderbook

        def _call(_):
            r = config.objconn.orderBook()
            if r is None:
                # No response at all -> genuine failure, let _retry try again.
                raise ValueError('no response from orderBook')
            # data may legitimately be None/empty when there are no orders.
            return r.get('data') or []

        config.orderbook = _retry(_call, 'orderBook', retries=3) or []
        _last_orderbook_ts = time.monotonic()
        return config.orderbook


def refresh_positions(force=False):
    if getattr(config, 'DRY_RUN', False):
        # Paper trading: no real positions at the broker, skip the API call.
        return config.positions or []

    global _last_position_ts
    with _position_lock:
        age = time.monotonic() - _last_position_ts
        if not force and config.positions is not None and age < _POSITION_TTL:
            return config.positions

        def _call(_):
            r = config.objconn.position()
            if r is None:
                # No response at all -> genuine failure, let _retry try again.
                raise ValueError('no response from position')
            # data may legitimately be None/empty when there are no open positions.
            return r.get('data') or []

        config.positions = _retry(_call, 'position', retries=3) or []
        _last_position_ts = time.monotonic()
        return config.positions


def cancel_all_pending():
    """Cancel every open/pending order (deliverable: cancel all pending orders)."""
    for o in refresh_orderbook():
        if str(o.get('status', '')).lower() in ('open', 'pending', 'trigger pending', 'modified'):
            cancel(o.get('orderid'))


def cancel_pending_for_tokens(tokens):
    """
    Cancel only the open/pending orders whose symboltoken is in `tokens`.

    Used at the 14:14 morning exit so Strategy-1 pending orders are cancelled
    without ever touching the hedge orders.
    """
    wanted = {str(t) for t in tokens}
    for o in refresh_orderbook():
        if str(o.get('symboltoken')) in wanted and \
                str(o.get('status', '')).lower() in ('open', 'pending', 'trigger pending', 'modified'):
            cancel(o.get('orderid'))


