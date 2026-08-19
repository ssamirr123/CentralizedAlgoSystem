import config,threading,time,token_file,rest_func
import pandas as pd
from datetime import datetime


def update_candle(token,ltp,volume):
    key = str(token)
    if key not in config.tlv_data:          # ignore ticks for tokens we haven't subscribed/initialised yet
        return
    config.tlv_data[key]['minute'].append(datetime.now().minute)
    config.tlv_data[key]['ltp'].append(ltp)
    config.tlv_data[key]['volume'].append(volume)

def clear_tlv_data(token):
    config.tlv_data[str(token)] = {'minute':[],'ltp': [], 'volume': []}

def clear_ohlc_data(token):
    config.ohlc_data[str(token)] = {'Datetime':[] ,'Open': [], 'High': [],'Low': [], 'Close':[], 'Volume':[]}

def convert_2min(dff):
    if dff is None:                                            # NEW
        print('[WARNING] convert_2min: received None – skipping')  # NEW
        return None                                            # NEW
    if dff.empty:                                              # NEW
        print('[WARNING] convert_2min: received empty DataFrame – skipping')  # NEW
        return None
    dff = dff.copy()
    dff.columns = ['Datetime', 'Open', 'High', 'Low', 'Close', 'Volume']
    dff['Datetime'] = pd.to_datetime(dff['Datetime'], format="%Y-%m-%dT%H:%M:%S%z")
    dff.set_index('Datetime', inplace=True)
    dff.index = dff.index + pd.Timedelta(minutes=1)
    ohlc_dict = {
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum'
    }
    dff = dff.resample('2T').agg(ohlc_dict).dropna()
    dff.index = dff.index.strftime('%H:%M:%S')
    dff.reset_index(inplace=True)
    return dff

def add_previous_data(strike):
    token = token_file.token_nifty(strike)[1]
    raw   = rest_func.get_option_ohlc(token)
    temp  = convert_2min(raw)
    if temp is None:                                           # NEW
        print(f'[WARNING] No historical data for {strike} – starting with live data only')  # NEW
        return                                                 # NEW
    config.ohlc_data[token] = {col: temp[col].tolist() for col in temp.columns}
    print(f'[OK] Loaded {len(temp)} candles for {strike}')    # NEW

def get_previous_minute(minute):
    return (minute - 1) % 60

def savedata_ohlc(token):
    time.sleep(120)
    while True:
        dt = datetime.now()
        if((dt.minute%2==0 and dt.second==59 and dt.microsecond>750000)):
            try:
                temp = config.tlv_data[token]
                min_length = min(len(temp['minute']), len(temp['ltp']), len(temp['volume']))
                temp = pd.DataFrame({
                    'minute': temp['minute'][:min_length],
                    'ltp': temp['ltp'][:min_length],
                    'volume': temp['volume'][:min_length]
                })
                temp = temp[temp['minute'].isin([dt.minute, get_previous_minute(dt.minute)])][['ltp', 'volume']].reset_index(drop=True)
                if len(temp) == 0:
                    # No ticks arrived in this 2-min window. Carry forward the previous close as a
                    # flat (0-volume) candle so the candle time-grid stays intact and no thread crashes.
                    prev_close = config.ohlc_data[token]['Close'][-1] if config.ohlc_data[token]['Close'] else 0
                    open_ = high = low = close = prev_close
                    vol = 0
                else:
                    open_ = temp['ltp'][0]
                    high = max(temp['ltp'])
                    low = min(temp['ltp'])
                    close = temp['ltp'].iloc[-1]
                    vol = (temp['volume'].iloc[-1]) - (temp['volume'][0])
                config.ohlc_data[token]['Datetime'].append(dt.strftime('%H:%M:')+'00')
                config.ohlc_data[token]['Open'].append(open_)
                config.ohlc_data[token]['High'].append(high)
                config.ohlc_data[token]['Low'].append(low)
                config.ohlc_data[token]['Close'].append(close)
                config.ohlc_data[token]['Volume'].append(vol)
                clear_tlv_data(token)
            except Exception as e:
                print(f'[ERROR] savedata_ohlc({token}): {e} – skipping this candle')
            time.sleep(118)
        time.sleep(0.01)

def subscribe_token(token):
    token_list = [{"exchangeType":2,"tokens":[token]}]
    config.sws.subscribe('test1', 2, token_list)
    config.tlv_data[str(token)] = {'minute':[],'ltp': [],'volume': []}
    config.ohlc_data[str(token)] = {'Datetime':[] ,'Open': [], 'High': [],'Low': [], 'Close':[], 'Volume':[]}
    threading.Thread(target=savedata_ohlc,args=(token,)).start()
