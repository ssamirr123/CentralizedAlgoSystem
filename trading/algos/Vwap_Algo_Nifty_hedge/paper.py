"""
Paper-trading (DRY_RUN) simulator for Vwap_Algo_Nifty_hedge.

When config.DRY_RUN is True, rest_func routes every order call here instead
of the broker:
  * market orders fill immediately at the latest websocket LTP
  * stop-loss orders are held locally and "fill" when LTP reaches the trigger
    (checked by rest_func.sltracking), since there is no broker order book
    to watch in paper mode
  * every fill goes into an in-memory ledger that monitor.py turns into
    broker-shaped position rows, so the dashboard's P&L / Positions work the
    same way in paper and live
Never touches the broker.
"""
import threading
import time
from datetime import datetime

import config

_lock = threading.Lock()
_fills = []        # {symbol, token, side, qty, price, time}
_sl_orders = {}    # order_id -> {symbol, token, qty, trigger, status}
_last_price = {}   # symbol -> last fill price (LTP fallback for reporting)
_symbol_token = {} # symbol -> token


def _new_id(prefix):
    return f'{prefix}-{int(time.time() * 1000)}'


def ltp(token, symbol=None):
    """Latest traded price: websocket tick / candle for subscribed tokens,
    else (e.g. the hedge leg, which is never subscribed) a read-only REST
    ltpData quote. Returns None if no price is available."""
    key = str(token)
    try:
        ticks = config.tlv_data.get(key, {}).get('ltp') or []
        if ticks:
            return float(ticks[-1])
        closes = config.ohlc_data.get(key, {}).get('Close') or []
        if closes:
            return float(closes[-1])
    except Exception:
        pass
    if symbol:
        try:
            return float(config.objconn.ltpData('NFO', str(symbol), key)['data']['ltp'])
        except Exception as e:
            print(f'[DRY_RUN] ltpData({symbol}) failed: {e}')
    return None


def _record_fill(symbol, token, side, qty, price):
    with _lock:
        _fills.append({
            'symbol': str(symbol), 'token': str(token), 'side': side.upper(),
            'qty': int(qty), 'price': float(price), 'time': datetime.now(),
        })
        _last_price[str(symbol)] = float(price)
        _symbol_token[str(symbol)] = str(token)


def market_order(symbol, token, qty, side):
    price = ltp(token, symbol)
    if price is None:
        print(f'[DRY_RUN] market {side} {symbol}: no LTP yet -> order NOT simulated')
        return None
    _record_fill(symbol, token, side, qty, price)
    # Buying back a short closes it, so any stop-loss still resting on that
    # symbol must not fire afterwards.
    if side.upper() == 'BUY':
        with _lock:
            for sl in _sl_orders.values():
                if sl['symbol'] == str(symbol) and sl['status'] == 'open':
                    sl['status'] = 'cancelled'
    order_id = _new_id('DRYRUN')
    print(f'[DRY_RUN] market {side.upper()} {symbol} qty={qty} SIMULATED fill @ {price} -> {order_id}')
    return order_id


def stoploss_order(symbol, token, qty, trigger):
    order_id = _new_id('DRYRUN-SL')
    with _lock:
        _sl_orders[order_id] = {
            'symbol': str(symbol), 'token': str(token), 'qty': int(qty),
            'trigger': float(trigger), 'status': 'open',
        }
    print(f'[DRY_RUN] stoploss BUY {symbol} qty={qty} trigger={trigger} held locally -> {order_id}')
    return order_id


def modify_stoploss(order_id, trigger):
    with _lock:
        sl = _sl_orders.get(str(order_id))
        if sl and sl['status'] == 'open':
            sl['trigger'] = float(trigger)
    print(f'[DRY_RUN] stoploss {order_id} trigger -> {trigger}')
    return order_id


def check_stoploss(order_id):
    """Return 'complete' once LTP has reached the trigger (fills it),
    'cancelled' if it was cancelled, else 'open'."""
    with _lock:
        sl = _sl_orders.get(str(order_id))
        if sl is None:
            return 'cancelled'
        if sl['status'] != 'open':
            return sl['status']
        token, symbol, qty, trigger = sl['token'], sl['symbol'], sl['qty'], sl['trigger']
    price = ltp(token)
    if price is None or price < trigger:
        return 'open'
    with _lock:
        if sl['status'] != 'open':
            return sl['status']
        sl['status'] = 'complete'
    _record_fill(symbol, token, 'BUY', qty, price)
    print(f'[DRY_RUN] stoploss {order_id} {symbol} HIT: SIMULATED BUY qty={qty} @ {price} (trigger {trigger})')
    return 'complete'


def positions():
    """Broker-shaped position rows (the keys monitor.py already reads from
    AngelOne's position book), built from the paper ledger."""
    with _lock:
        fills = list(_fills)
        last_price = dict(_last_price)
        symbol_token = dict(_symbol_token)

    by_symbol = {}
    for f in fills:
        s = by_symbol.setdefault(f['symbol'], {'bq': 0, 'bv': 0.0, 'sq': 0, 'sv': 0.0})
        if f['side'] == 'BUY':
            s['bq'] += f['qty']; s['bv'] += f['qty'] * f['price']
        else:
            s['sq'] += f['qty']; s['sv'] += f['qty'] * f['price']

    rows = []
    for symbol, s in by_symbol.items():
        mark = ltp(symbol_token.get(symbol))   # websocket only: no REST call per report
        if mark is None:
            mark = last_price.get(symbol, 0.0)
        netqty = s['bq'] - s['sq']
        pnl = s['sv'] - s['bv'] + netqty * mark
        avg_buy = s['bv'] / s['bq'] if s['bq'] else 0.0
        avg_sell = s['sv'] / s['sq'] if s['sq'] else 0.0
        realised = min(s['bq'], s['sq']) * (avg_sell - avg_buy)
        rows.append({
            'tradingsymbol': symbol,
            'netqty': netqty,
            'avgnetprice': avg_buy if netqty > 0 else avg_sell if netqty < 0 else 0.0,
            'ltp': mark,
            'realised': round(realised, 2),
            'unrealised': round(pnl - realised, 2),
            'pnl': round(pnl, 2),
        })
    return rows


def trade_count():
    with _lock:
        return len(_fills)
