import log_setup
log_setup.init()   # mirror all print()/stderr output to logs/<date>/app.log

import warnings
import threading
import time
warnings.filterwarnings('ignore')

import config
import connectapi
import token_file
import websocket_feed as wf
import monitor
from strategy.engine import run

# Best-effort heartbeat agent (never affects trading).
monitor.start()

try:
    token_file.download_token()

    # Keep retrying login until we have a valid session - running with objconn=None
    # would make the websocket and every REST call fail for the whole day.
    config.objconn = connectapi.makeconnection()
    while config.objconn is None:
        print('[STARTUP] No broker session yet - retrying connection in 5s...')
        time.sleep(5)
        config.objconn = connectapi.makeconnection()

    # Live market data feed (auto-reconnecting daemon).
    threading.Thread(target=wf.connect, daemon=True).start()
    time.sleep(3)   # give the socket a moment to open

    # Blocks until the trading day ends (15:25) or an emergency square-off.
    run()

except KeyboardInterrupt:
    print('[SHUTDOWN] KeyboardInterrupt - stopping strategy')
    monitor.stop('STOPPED')
    raise
except Exception as e:
    print(f'[FATAL] Unhandled exception in main: {e}')
    monitor.stop('ERROR')
    raise

