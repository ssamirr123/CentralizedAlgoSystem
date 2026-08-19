from datetime import datetime
import pandas as pd
import pandas_ta as ta
import config,make_data,token_file,time,threading,manager

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
                print(f'[RATE LIMIT] {label} attempt {attempt+1}/{retries} – waiting {delay}s...')
            else:
                print(f'[ERROR] {label}: {e} – attempt {attempt+1}/{retries}')
        time.sleep(delay)
        delay = min(delay * 2, 30)   # cap at 30 s
    print(f'[FAILED] {label} – all {retries} retries exhausted, returning None')
    return None

def get_nifty_atm():
        def _call():
            nifty_ltp = config.objconn.ltpData('NSE','NIFTY','99926000')['data']['ltp']
            nifty_atm = round(nifty_ltp/50)*50
            return nifty_atm
        return _retry_call(_call, retries=5, base_delay=3, label=f'get_nifty_atm()')


# def get_nifty_close():
#     date = datetime.today().strftime('%Y-%m-%d')
#     historicparam = {
#          "exchange": "NSE",
#          "symboltoken": "99926000",
#          "interval": "ONE_MINUTE",
#          "fromdate": date+" 09:15",
#          "todate": date+" 15:30"
#     }
#     for attempt in range(0, 3):
#         try:
#             temp = pd.DataFrame(config.objconn.getCandleData(historicparam)['data'])
#             temp = temp[4].iloc[-2]
#             return round(temp/50)*50
#         except Exception as e:
#             print(e,'_Error Resolved')

def get_nifty_close():
    date = datetime.today().strftime('%Y-%m-%d')
    historicparam = {
        "exchange": "NSE",
        "symboltoken": "99926000",
        "interval": "ONE_MINUTE",
        "fromdate": date + " 09:15",
        "todate": date + " 15:30"
    }

    def _call():
        temp = pd.DataFrame(config.objconn.getCandleData(historicparam)['data'])
        temp = temp[4].iloc[-2]
        return round(temp/50)*50

    return _retry_call(_call, retries=5, base_delay=1, label=f'get_nifty_close()')


def get_option_ohlc(token):
    date = datetime.today().strftime('%Y-%m-%d')
    historicparam = {
         "exchange": "NFO",
         "symboltoken": str(token),
         "interval": "ONE_MINUTE",
         "fromdate": date+" 09:15",
         "todate": date+" 15:30"
    }
    def _call():
        temp = pd.DataFrame(config.objconn.getCandleData(historicparam)['data'])
        if temp.empty:
            raise ValueError('Empty DataFrame received')
        return temp.iloc[:-1]
    return _retry_call(_call, retries=5, base_delay=3, label=f'get_option_ohlc({token})')


def make_vwap(dff):
    dff = dff.copy()
    dff['Datetime'] = pd.to_datetime(dff['Datetime'], format='%H:%M:%S')
    numeric_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    dff[numeric_cols] = dff[numeric_cols].astype(float)
    dff.set_index('Datetime', inplace=True)
    dff['Vwap'] = ta.vwap(dff['High'], dff['Low'], dff['Close'], dff['Volume'])
    dff.index = dff.index.strftime('%H:%M:%S')
    dff.reset_index(inplace=True)
    return dff

def place_market_order(symbol,token,qty,ordertype):
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
        print('Order placed',symbol)
        return order_id

    return _retry_call(_call, retries=5, base_delay=1, label=f'place_market_order({symbol},{ordertype})')

def place_stoploss_order(symbol,token,qty,stoploss):
    stoploss = round(float(stoploss))
    price = stoploss+2
    orderparams = {
        "variety": "STOPLOSS",
        "tradingsymbol": str(symbol),
        "symboltoken": str(token),
        "transactiontype": "BUY",
        "exchange": "NFO",
        "ordertype": "STOPLOSS_LIMIT",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": str(price),
        "triggerprice":str(stoploss),
        "quantity": str(qty)
    }

    def _call():
        order_id = config.objconn.placeOrder(orderparams)
        if order_id is None:
            raise ValueError('Stoploss order response missing order id')
        print('Stoploss Order placed',symbol)
        return order_id

    return _retry_call(_call, retries=5, base_delay=1, label=f'place_stoploss_order({symbol})')

def modify_stoploss_order(symbol,token,qty,stoploss,orderid):
    stoploss = round(float(stoploss))
    price = stoploss+2
    orderparams = {
        "variety": "STOPLOSS",
        "orderid": str(orderid),
        "tradingsymbol": str(symbol),
        "symboltoken": str(token),
        "exchange": "NFO",
        "ordertype": "STOPLOSS_LIMIT",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": str(price),
        "triggerprice":str(stoploss),
        "quantity": str(qty)
    }

    def _call():
        order_id = config.objconn.modifyOrder(orderparams)
        if order_id is None:
            raise ValueError('Modify stoploss response missing order id')
        print('Stoploss Order placed',symbol)
        return order_id

    return _retry_call(_call, retries=5, base_delay=1, label=f'modify_stoploss_order({symbol})')

def order_info(orderid,orderbook):
    if not orderbook:                       # None or empty -> nothing to look up yet
        return None
    for i in orderbook:
        if i['orderid'] == orderid:
            return i['averageprice'],i['transactiontype'],i['strikeprice'],i['status']
    return None

def sltracking(order_id,token):
    if order_id is None:                    # order failed to place -> can't track it
        print('[WARNING] sltracking: order_id is None – stoploss cannot be tracked')
        return
    while True:
        info = order_info(str(order_id),config.orderbook)
        if info is not None and info[3] == 'complete':
            config.slhit[token] = True
            break
        time.sleep(1)

def get_hedge_strike(symbol):
    # Example:
    # NIFTY19MAY2623700CE
    # Extract expiry string
    expiry_str = symbol[5:12]   # 19MAY26
    # Convert to date
    expiry_date = datetime.strptime(expiry_str, '%d%b%y').date()
    # Today's date
    today = datetime.today().date()
    # Check expiry day
    is_expiry_day = (expiry_date == today)
    # Extract strike type
    option_type = symbol[-2:]   # CE / PE
    # Extract strike price
    strike = int(symbol[12:-2])
    # Hedge logic
    hedge_gap = 500 if is_expiry_day else 1000
    # Create hedge strike
    if option_type == 'CE':
        hedge_strike = strike + hedge_gap
    elif option_type == 'PE':
        hedge_strike = strike - hedge_gap
    return str(hedge_strike) + option_type

def get_orderbook():
    def _call():
        response = config.objconn.orderBook()
        data = response.get('data') if response else None
        if data is None:
            raise ValueError('Order book response missing data')
        return data

    result = _retry_call(_call, retries=5, base_delay=3, label='get_orderbook()')
    if result is None:
        print('[FAILED] get_orderbook – returning empty list')
        return []   # keep it iterable so order_info/sltracking never crash
    return result

def saveorderbook():
    while True:
        config.orderbook = get_orderbook()
        time.sleep(1)

def add_make_option():
    nifty_atm = get_nifty_close()
    ce_strike = str(nifty_atm)+'CE'
    pe_strike = str(nifty_atm)+'PE'
    print(ce_strike,pe_strike)
    make_data.subscribe_token(token_file.token_nifty(ce_strike)[1])
    make_data.subscribe_token(token_file.token_nifty(pe_strike)[1])
    time.sleep(120)
    make_data.add_previous_data(ce_strike)
    make_data.add_previous_data(pe_strike)
    threading.Thread(target=manager.trademanager,args=(ce_strike,)).start()
    time.sleep(0.1)
    threading.Thread(target=manager.trademanager,args=(pe_strike,)).start()
    threading.Thread(target=saveorderbook).start()
