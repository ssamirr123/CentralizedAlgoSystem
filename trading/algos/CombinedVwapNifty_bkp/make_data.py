import config, threading, time, token_file, rest_func
import pandas as pd
from datetime import datetime


def update_candle(token, ltp, day_volume):
    key = str(token)
    # Always keep the "latest known price" fresh (used by risk_manager's
    # tick-by-tick loss checks and by the stale-feed watchdog), even before
    # the token has been subscribed via subscribe_token().
    config.last_ltp[key] = ltp
    config.last_tick_time[key] = time.time()
    if key not in config.tlv_data:          # ignore ticks for tokens we haven't subscribed/initialised yet
        return
    config.tlv_data[key]['minute'].append(datetime.now().minute)
    config.tlv_data[key]['ltp'].append(ltp)
    config.tlv_data[key]['volume'].append(day_volume)


def clear_tlv_data(token):
    config.tlv_data[str(token)] = {'minute': [], 'ltp': [], 'volume': []}


def clear_ohlc_data(token):
    config.ohlc_data[str(token)] = {'Datetime': [], 'Open': [], 'High': [], 'Low': [], 'Close': [], 'Volume': []}


def convert_1min(dff):
    """Historic REST candles already arrive at ONE_MINUTE granularity - just
    reshape them into the same dict-of-lists layout used by config.ohlc_data."""
    if dff is None or dff.empty:
        print('[WARNING] convert_1min: received empty/None data - skipping')
        return None
    dff = dff.copy()
    dff.columns = ['Datetime', 'Open', 'High', 'Low', 'Close', 'Volume']
    dff['Datetime'] = pd.to_datetime(dff['Datetime'], format="%Y-%m-%dT%H:%M:%S%z")
    dff['Datetime'] = dff['Datetime'].dt.strftime('%H:%M:00')
    return dff


def add_previous_data(strike):
    symbol, token, lotsize = token_file.token_nifty(strike)
    raw = rest_func.get_option_ohlc(token)
    temp = convert_1min(raw)
    if temp is None:
        print(f'[WARNING] No historical data for {strike} - starting with live data only')
        return
    config.ohlc_data[token] = {col: temp[col].tolist() for col in temp.columns}
    print(f'[OK] Loaded {len(temp)} candles for {strike}')


def savedata_ohlc(token):
    """
    Builds true 1-minute candles from live ticks: at HH:MM:59 finalise the
    candle for the *current* minute (HH:MM:00) using every tick seen since
    the last flush, then clear the buffer for the next minute.
    """
    time.sleep(60)   # let the first full minute of ticks accumulate
    while True:
        dt = datetime.now()
        if dt.second == 59 and dt.microsecond > 750000:
            try:
                temp = config.tlv_data[token]
                min_length = min(len(temp['minute']), len(temp['ltp']), len(temp['volume']))
                ltp_list = temp['ltp'][:min_length]
                vol_list = temp['volume'][:min_length]
                if len(ltp_list) == 0:
                    # No ticks this minute -> carry forward the previous close as a
                    # flat (0-volume) candle so the time-grid stays intact.
                    prev_close = config.ohlc_data[token]['Close'][-1] if config.ohlc_data[token]['Close'] else 0
                    open_ = high = low = close = prev_close
                    vol = 0
                else:
                    open_ = ltp_list[0]
                    high = max(ltp_list)
                    low = min(ltp_list)
                    close = ltp_list[-1]
                    vol = (vol_list[-1] - vol_list[0]) if len(vol_list) > 1 else 0
                config.ohlc_data[token]['Datetime'].append(dt.strftime('%H:%M:00'))
                config.ohlc_data[token]['Open'].append(open_)
                config.ohlc_data[token]['High'].append(high)
                config.ohlc_data[token]['Low'].append(low)
                config.ohlc_data[token]['Close'].append(close)
                config.ohlc_data[token]['Volume'].append(vol)
                clear_tlv_data(token)
            except Exception as e:
                print(f'[ERROR] savedata_ohlc({token}): {e} - skipping this candle')
            time.sleep(58)
        time.sleep(0.01)


def subscribe_token(token):
    token_list = [{"exchangeType": 2, "tokens": [token]}]
    config.sws.subscribe('straddle', 2, token_list)
    config.tlv_data[str(token)] = {'minute': [], 'ltp': [], 'volume': []}
    config.ohlc_data[str(token)] = {'Datetime': [], 'Open': [], 'High': [], 'Low': [], 'Close': [], 'Volume': []}
    threading.Thread(target=savedata_ohlc, args=(token,)).start()
