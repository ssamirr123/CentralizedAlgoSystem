import log_setup
log_setup.init()   # mirror all print()/stderr output to logs/<date>/app.log

import sys
import warnings
import threading
import time
from pathlib import Path
warnings.filterwarnings('ignore')

# trading_agent.py's START_ALGO only marks this process RUNNING once it
# sees a self-written PID file (write_pid_file) -- without it, it always
# times out after 2s and reports ERROR ("process exited immediately
# after launch") no matter how healthy the actual process is. This algo
# came from its own separate repo (not the trading/algos/example_strategy
# template every other algo here is copied from), so it never had this
# control-framework contract wired in.
_project_root = Path(__file__).resolve().parents[3]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
from trading.common.utils import write_pid_file  # noqa: E402
write_pid_file("DoubleStraddelAlgo")

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

