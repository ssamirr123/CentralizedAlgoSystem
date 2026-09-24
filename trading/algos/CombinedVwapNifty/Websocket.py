import config, make_data, time


def ConnectSocket_Angel():
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    while True:
        try:
            if not config.objconn:
                time.sleep(1)
                continue
            config.sws = SmartWebSocketV2(config.token, config.apikey, config.clientid, config.objconn.getfeedToken())

            def on_open(wsapp):
                print('[ANGEL WS] Socket is Open')

            def on_data(wsapp, message):
                try:
                    make_data.update_candle(message['token'], message['last_traded_price'] / 100,
                                             message['volume_trade_for_the_day'])
                except Exception as e:
                    print(f'[ERROR] on_data: {e}')

            def on_error(wsapp, error):
                print(f'[ANGEL WS ERROR] {error}')

            def on_close(wsapp):
                print('[ANGEL WS CLOSED] will attempt to reconnect...')

            config.sws.on_open = on_open
            config.sws.on_data = on_data
            config.sws.on_error = on_error
            config.sws.on_close = on_close
            config.sws.connect()   # blocks until the socket drops
        except Exception as e:
            print(f'[ANGEL WS] connection error: {e}')
        print('[ANGEL WS] reconnecting in 5s...')
        time.sleep(5)


def ConnectSocket_Shoonya():
    while True:
        try:
            if not config.objconn:
                time.sleep(1)
                continue

            def on_open(*args, **kwargs):
                print('[SHOONYA WS] Socket is Open')
                # Auto-resubscribe if tokens are already selected
                if config.ce_token and hasattr(config.objconn, 'subscribe'):
                    config.objconn.subscribe(f"NFO|{config.ce_token}")
                    print(f'[SHOONYA WS] Resubscribed CE: NFO|{config.ce_token}')
                if config.pe_token and hasattr(config.objconn, 'subscribe'):
                    config.objconn.subscribe(f"NFO|{config.pe_token}")
                    print(f'[SHOONYA WS] Resubscribed PE: NFO|{config.pe_token}')

            def on_data(message, *args, **kwargs):
                try:
                    token = str(message.get('tk', ''))
                    if not token:
                        return
                    if 'lp' in message and message['lp'] is not None:
                        config.last_ltp[token] = float(message['lp'])
                    if 'v' in message and message['v'] is not None:
                        config.last_volume[token] = float(message['v'])

                    ltp = config.last_ltp.get(token, 0.0)
                    vol = config.last_volume.get(token, 0.0)
                    if ltp > 0:
                        make_data.update_candle(token, ltp, vol)
                except Exception as e:
                    print(f'[ERROR] Shoonya on_data: {e}')

            def on_error(error=None, *args, **kwargs):
                print(f'[SHOONYA WS ERROR] {error}')

            def on_close(*args, **kwargs):
                print('[SHOONYA WS CLOSED]')

            config.objconn.start_websocket(
                subscribe_callback=on_data,
                socket_open_callback=on_open,
                socket_error_callback=on_error,
                socket_close_callback=on_close
            )
            print('[SHOONYA WS] WebSocket thread started')
            # Keep supervisor thread alive
            while True:
                time.sleep(10)
        except Exception as e:
            print(f'[SHOONYA WS] connection error: {e}')
            time.sleep(5)


def ConnectSocket():
    if config.BROKER == "SHOONYA":
        ConnectSocket_Shoonya()
    else:
        ConnectSocket_Angel()

