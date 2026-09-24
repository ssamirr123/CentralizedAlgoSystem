import config, make_data, time
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2


def ConnectSocket():
    # Reconnect forever so a dropped feed never silently stops the data flow.
    while True:
        try:
            config.sws = SmartWebSocketV2(config.token, config.apikey, config.clientid, config.objconn.getfeedToken())

            def on_open(wsapp):
                print('Socket is Open')

            def on_data(wsapp, message):
                try:
                    make_data.update_candle(message['token'], message['last_traded_price'] / 100,
                                             message['volume_trade_for_the_day'])
                except Exception as e:
                    print(f'[ERROR] on_data: {e}')

            def on_error(wsapp, error):
                print(f'[WS ERROR] {error}')

            def on_close(wsapp):
                print('[WS CLOSED] will attempt to reconnect...')

            config.sws.on_open = on_open
            config.sws.on_data = on_data
            config.sws.on_error = on_error
            config.sws.on_close = on_close
            config.sws.connect()   # blocks until the socket drops
        except Exception as e:
            print(f'[WS] connection error: {e}')
        # connect() returned / crashed -> wait and reconnect (library resubscribes tokens).
        print('[WS] reconnecting in 5s...')
        time.sleep(5)
