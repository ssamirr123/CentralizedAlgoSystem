import config
import time
from SmartApi.smartWebSocketV2 import SmartWebSocketV2


def _on_data(wsapp, message):
    """Cache the latest LTP per token. Feed prices are in paise -> divide by 100."""
    try:
        config.ltp[str(message['token'])] = message['last_traded_price'] / 100.0
    except Exception as e:
        print(f'[WS ERROR] on_data: {e}')


def _on_open(wsapp):
    print('[WS] socket open')
    # Each reconnect gets a fresh SmartWebSocketV2 object (empty subscription
    # list), so the index spot token must be re-subscribed here every time the
    # socket opens - otherwise get_atm() silently loses its live tick feed and
    # falls back to the network-timeout-bound REST path on every call.
    subscribe(config.INDEX_TOKEN, exchange_type=1)


def connect():
    """
    Reconnect forever so a dropped feed never silently stops the data flow
    (WebSocket disconnect handling). The library resubscribes option tokens on
    reconnect; the index token is re-subscribed explicitly in _on_open above.
    """
    # Imported lazily so the rest of the project (tests/backtest) can import this
    # module without the SmartApi websocket dependency installed.
    while True:
        try:
            config.sws = SmartWebSocketV2(
                config.token, config.apikey, config.clientid,
                config.objconn.getfeedToken()
            )
            config.sws.on_open = _on_open
            config.sws.on_data = _on_data
            config.sws.on_error = lambda w, e: print(f'[WS ERROR] {e}')
            config.sws.on_close = lambda w: print('[WS] closed - will reconnect')
            config.sws.connect()   # blocks until the socket drops
        except Exception as e:
            print(f'[WS] connection error: {e}')
        print('[WS] reconnecting in 5s...')
        time.sleep(5)


def subscribe(token, exchange_type=2):
    """Subscribe a single token in LTP mode (mode 1). exchangeType 2=NFO options, 1=NSE cash/index."""
    try:
        token_list = [{"exchangeType": exchange_type, "tokens": [str(token)]}]
        config.sws.subscribe('feed', 1, token_list)
        print(f'[WS] subscribed {token} (exchangeType={exchange_type})')
    except Exception as e:
        print(f'[WS] subscribe {token} failed: {e}')


def get_ltp(token, fallback_rest=True, exchange=None):
    """
    Return the live LTP from the WebSocket cache, falling back to a REST quote
    (broker/API failure handling) if the feed hasn't delivered a tick yet.

    `exchange` defaults to OPT_EXCH (NFO) since most callers query option
    tokens; pass config.INDEX_EXCH explicitly for the NSE spot index token.
    """
    t = str(token)
    if t in config.ltp:
        return config.ltp[t]
    if fallback_rest and config.objconn is not None:
        try:
            return config.objconn.ltpData(exchange or config.OPT_EXCH, '', t)['data']['ltp']
        except Exception as e:
            print(f'[WS] LTP REST fallback failed for {t}: {e}')
    return None


def wait_ltp(token, timeout=10, fallback_rest=True, exchange=None):
    """
    Block up to `timeout` seconds for a valid (non-None) LTP for `token`.

    Used at entry so SL/target are never computed against a missing price.
    Returns the LTP, or None if none arrived within the timeout. Pass
    fallback_rest=False to poll only the WS cache (no REST calls per tick) -
    e.g. when the caller does its own single, correctly-exchanged REST
    fallback afterward.
    """
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        ltp = get_ltp(token, fallback_rest=fallback_rest, exchange=exchange)
        if ltp is not None:
            return ltp
        _t.sleep(0.5)
    print(f'[WS] wait_ltp timed out for {token} after {timeout}s')
    return None



