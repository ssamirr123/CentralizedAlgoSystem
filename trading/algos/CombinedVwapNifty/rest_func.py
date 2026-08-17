from datetime import datetime
import time
import threading
import pandas as pd
import config, make_data, token_file


def _retry_call(fn, retries=5, base_delay=2, label=''):
    """
    Call fn() up to `retries` times with exponential back-off.
    Returns the result on success, or None if all attempts fail.
    """
    delay = base_delay
    for attempt in range(retries):
        try:
            result = fn()
            if result is not None:
                return result
        except Exception as e:
            err = str(e)
            if 'exceeding access rate' in err or 'Access denied' in err:
                print(f'[RATE LIMIT] {label} attempt {attempt+1}/{retries} - waiting {delay}s...')
            else:
                print(f'[ERROR] {label}: {e} - attempt {attempt+1}/{retries}')
        time.sleep(delay)
        delay = min(delay * 2, 30)   # cap at 30 s
    print(f'[FAILED] {label} - all {retries} retries exhausted, returning None')
    return None


# --------------------------------------------------------------------------- #
# Market data helpers
# --------------------------------------------------------------------------- #
def get_nifty_atm():
    def _call():
        nifty_ltp = config.objconn.ltpData('NSE', 'NIFTY', '99926000')['data']['ltp']
        nifty_atm = round(nifty_ltp / 50) * 50
        return nifty_atm
    return _retry_call(_call, retries=5, base_delay=3, label='get_nifty_atm()')


def get_option_ohlc(token):
    date = datetime.today().strftime('%Y-%m-%d')
    historicparam = {
        "exchange": "NFO",
        "symboltoken": str(token),
        "interval": "ONE_MINUTE",
        "fromdate": date + " 09:15",
        "todate": date + " 15:30"
    }
    def _call():
        temp = pd.DataFrame(config.objconn.getCandleData(historicparam)['data'])
        if temp.empty:
            raise ValueError('Empty DataFrame received')
        return temp.iloc[:-1]
    return _retry_call(_call, retries=5, base_delay=3, label=f'get_option_ohlc({token})')


def get_rest_ltp(token):
    """REST fallback LTP, used by manager.py when the WebSocket feed for a
    leg is flagged stale."""
    def _call():
        return config.objconn.ltpData('NFO', '', str(token))['data']['ltp']
    return _retry_call(_call, retries=2, base_delay=1, label=f'get_rest_ltp({token})')


def make_cp_cv(ce_df, pe_df):
    """
    Combined Premium (CP) + Combined VWAP (CV) from the two legs' 1-minute
    candles. Same construction as `rest_func.make_vwap` in the reference
    project (cumulative typical-price-weighted-by-volume VWAP), applied to
    the CE+PE combined premium series instead of a single leg. Computed
    manually (no pandas_ta dependency) to avoid that library's numba build
    issues on newer Python versions.
    """
    ce = ce_df.copy()
    pe = pe_df.copy()
    merged = pd.merge(ce, pe, on='Datetime', suffixes=('_CE', '_PE'), how='inner')
    if merged.empty:
        return merged

    merged['Open_CE'] = merged['Open_CE'].astype(float)
    merged['Open_PE'] = merged['Open_PE'].astype(float)
    merged['CP_Open'] = merged['Open_CE'] + merged['Open_PE']
    merged['CP_High'] = merged['High_CE'].astype(float) + merged['High_PE'].astype(float)
    merged['CP_Low'] = merged['Low_CE'].astype(float) + merged['Low_PE'].astype(float)
    merged['CP_Close'] = merged['Close_CE'].astype(float) + merged['Close_PE'].astype(float)
    merged['CP_Volume'] = merged['Volume_CE'].astype(float) + merged['Volume_PE'].astype(float)

    typical_price = (merged['CP_High'] + merged['CP_Low'] + merged['CP_Close']) / 3.0
    pv = typical_price * merged['CP_Volume']
    cum_pv = pv.cumsum()
    cum_vol = merged['CP_Volume'].cumsum()
    cv = (cum_pv / cum_vol).where(cum_vol > 0, merged['CP_Close'])

    merged['CP'] = merged['CP_Close'].round(2)
    merged['CV'] = cv.round(2)
    return merged[['Datetime', 'Close_CE', 'Close_PE', 'CP_Open', 'CP_High', 'CP_Low',
                   'CP_Close', 'CP_Volume', 'CP', 'CV']]


# --------------------------------------------------------------------------- #
# Order placement / order book
# --------------------------------------------------------------------------- #
def place_market_order(symbol, token, qty, ordertype):
    if config.DRY_RUN:
        fake_id = f'DRYRUN-{int(time.time()*1000)}'
        print(f'[DRY_RUN] place_market_order SKIPPED (paper) {symbol} {ordertype} qty={qty} -> {fake_id}')
        return fake_id
    orderparams = {
        "variety": "NORMAL",
        "tradingsymbol": str(symbol),
        "symboltoken": str(token),
        "transactiontype": ordertype.upper(),
        "exchange": "NFO",
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": '0',
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(qty)
    }

    def _call():
        order_id = config.objconn.placeOrder(orderparams)
        if order_id is None:
            raise ValueError('Market order response missing order id')
        print('Order placed', symbol)
        return order_id

    return _retry_call(_call, retries=5, base_delay=1, label=f'place_market_order({symbol},{ordertype})')


def place_limit_order(symbol, token, qty, ordertype, price):
    if config.DRY_RUN:
        fake_id = f'DRYRUN-{int(time.time()*1000)}'
        print(f'[DRY_RUN] place_limit_order SKIPPED (paper) {symbol} {ordertype} qty={qty} @ {price} -> {fake_id}')
        return fake_id
    orderparams = {
        "variety": "NORMAL",
        "tradingsymbol": str(symbol),
        "symboltoken": str(token),
        "transactiontype": ordertype.upper(),
        "exchange": "NFO",
        "ordertype": "LIMIT",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": str(round(price, 2)),
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(qty)
    }

    def _call():
        order_id = config.objconn.placeOrder(orderparams)
        if order_id is None:
            raise ValueError('Limit order response missing order id')
        print(f'Limit order placed {symbol} {ordertype} qty={qty} @ {price}')
        return order_id

    return _retry_call(_call, retries=3, base_delay=1, label=f'place_limit_order({symbol},{ordertype})')


def modify_limit_order(orderid, symbol, token, qty, price):
    if config.DRY_RUN:
        print(f'[DRY_RUN] modify_limit_order SKIPPED (paper) {symbol} {orderid} -> {price}')
        return orderid
    orderparams = {
        "variety": "NORMAL",
        "orderid": str(orderid),
        "tradingsymbol": str(symbol),
        "symboltoken": str(token),
        "exchange": "NFO",
        "ordertype": "LIMIT",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": str(round(price, 2)),
        "quantity": str(qty)
    }

    def _call():
        return config.objconn.modifyOrder(orderparams)

    return _retry_call(_call, retries=3, base_delay=1, label=f'modify_limit_order({symbol})')


def cancel_order(orderid):
    if config.DRY_RUN:
        print(f'[DRY_RUN] cancel_order SKIPPED (paper) {orderid}')
        return True
    try:
        config.objconn.cancelOrder(str(orderid), "NORMAL")
        return True
    except Exception as e:
        print(f'[ERROR] cancel_order({orderid}): {e}')
        return False


def order_info(orderid, orderbook):
    """Returns a dict {avg_price, side, status, filled_qty} or None if not
    (yet) present in the cached order book."""
    if not orderbook:
        return None
    for i in orderbook:
        if i['orderid'] == str(orderid):
            return {
                'avg_price': float(i.get('averageprice', 0) or 0),
                'side': i.get('transactiontype'),
                'status': str(i.get('status', '')).lower(),
                'filled_qty': int(i.get('filledshares', 0) or 0),
            }
    return None


def get_orderbook():
    def _call():
        response = config.objconn.orderBook()
        data = response.get('data') if response else None
        if data is None:
            raise ValueError('Order book response missing data')
        return data

    result = _retry_call(_call, retries=5, base_delay=3, label='get_orderbook()')
    if result is None:
        print('[FAILED] get_orderbook - returning empty list')
        return []
    return result


def saveorderbook():
    while True:
        if config.DRY_RUN:
            # No real orders in paper mode -> keep the cached book empty and
            # skip the REST polling entirely.
            config.orderbook = []
        else:
            config.orderbook = get_orderbook()
        time.sleep(config.RECONCILE_INTERVAL_SECONDS)


# --------------------------------------------------------------------------- #
# Robust limit-order execution: retries, repricing/chasing, partial fills,
# reconciliation (via config.orderbook, kept fresh by saveorderbook()), and
# slippage tracking.
# --------------------------------------------------------------------------- #
def execute_limit_order(symbol, token, qty, side, reference_price, label=''):
    """
    Places a LIMIT order and drives it to completion:
      * initial price offset from `reference_price` (favourable side)
      * waits up to ORDER_FILL_TIMEOUT_SECONDS for a fill (via config.orderbook,
        reconciled in the background by rest_func.saveorderbook)
      * on timeout: cancels and re-submits the *remaining* qty at a chased
        (worse) price, up to ORDER_MAX_REPRICE times
      * partial fills are handled naturally since only the remainder is
        resubmitted each attempt
    Returns (filled_qty, avg_fill_price, slippage). avg_fill_price/slippage
    are 0 if nothing filled.
    """
    # --- Paper-trading fast path: simulate an immediate full fill at the
    # reference price with zero slippage; never touches the broker. ---
    if config.DRY_RUN:
        fill_price = round(reference_price, 2)
        print(f'[DRY_RUN] {label} {symbol} {side} SIMULATED fill qty={qty} @ {fill_price} (paper)')
        return qty, fill_price, 0.0

    remaining = qty
    total_filled = 0
    weighted_price_sum = 0.0

    for attempt in range(config.ORDER_MAX_REPRICE + 1):
        if remaining <= 0:
            break
        offset = config.ORDER_LIMIT_OFFSET + attempt * config.ORDER_REPRICE_STEP
        if side.upper() == 'SELL':
            limit_price = max(0.05, reference_price + config.ORDER_LIMIT_OFFSET - attempt * config.ORDER_REPRICE_STEP)
        else:
            limit_price = max(0.05, reference_price - config.ORDER_LIMIT_OFFSET + attempt * config.ORDER_REPRICE_STEP)

        order_id = place_limit_order(symbol, token, remaining, side, limit_price)
        if order_id is None:
            print(f'[OrderMgr] {label} {symbol} {side} attempt {attempt+1}: placeOrder failed')
            continue

        deadline = time.time() + config.ORDER_FILL_TIMEOUT_SECONDS
        info = None
        while time.time() < deadline:
            info = order_info(order_id, config.orderbook)
            if info is not None and info['status'] in ('complete', 'rejected', 'cancelled'):
                break
            time.sleep(0.5)
        if info is None:
            info = {'avg_price': 0, 'status': 'open', 'filled_qty': 0}

        filled_this_round = info['filled_qty']
        if filled_this_round > 0:
            total_filled += filled_this_round
            weighted_price_sum += filled_this_round * (info['avg_price'] or limit_price)
            remaining = qty - total_filled
            slippage_now = round((info['avg_price'] or limit_price) - reference_price, 2)
            print(f'[OrderMgr] {label} {symbol} {side} filled {filled_this_round}/{qty} '
                  f'@ {info["avg_price"]} (slippage={slippage_now})')

        if info['status'] == 'complete' or remaining <= 0:
            break

        # Not (fully) filled within the timeout -> cancel remainder and chase price.
        cancel_order(order_id)
        print(f'[OrderMgr] {label} {symbol} {side} unfilled/partial '
              f'({filled_this_round}/{qty}) -> repricing (attempt {attempt+1}/{config.ORDER_MAX_REPRICE})')

    avg_price = round(weighted_price_sum / total_filled, 2) if total_filled else 0.0
    slippage = round(avg_price - reference_price, 2) if total_filled else 0.0
    if total_filled < qty:
        print(f'[OrderMgr] WARNING: {label} {symbol} {side} only filled {total_filled}/{qty} '
              f'after all retries.')
    return total_filled, avg_price, slippage


# --------------------------------------------------------------------------- #
# ATM lock + straddle setup (equivalent of the reference project's
# `add_make_option`)
# --------------------------------------------------------------------------- #
def setup_straddle():
    """
    Locks the ATM strike from the live NIFTY LTP, resolves the CE/PE
    contracts, subscribes both to the WebSocket feed, preloads today's
    historical 1-minute candles, and starts the trade manager.
    """
    import manager   # local import to avoid a circular import at module load time

    nifty_atm = get_nifty_atm()
    config.locked_strike = nifty_atm
    ce_strike = str(nifty_atm) + 'CE'
    pe_strike = str(nifty_atm) + 'PE'

    ce_symbol, ce_token, ce_lot = token_file.token_nifty(ce_strike)
    pe_symbol, pe_token, pe_lot = token_file.token_nifty(pe_strike)
    config.ce_symbol, config.ce_token = ce_symbol, ce_token
    config.pe_symbol, config.pe_token = pe_symbol, pe_token
    config.lot_size = ce_lot or config.lot_size
    config.qty = str(config.lot_size * config.num_lots)

    print(f'[ATM] Locked strike={nifty_atm}  CE={ce_symbol}({ce_token})  PE={pe_symbol}({pe_token})  '
          f'qty={config.qty}')

    make_data.subscribe_token(ce_token)
    make_data.subscribe_token(pe_token)
    time.sleep(2)
    make_data.add_previous_data(ce_strike)
    make_data.add_previous_data(pe_strike)

    threading.Thread(target=saveorderbook).start()
    threading.Thread(target=manager.trademanager).start()
