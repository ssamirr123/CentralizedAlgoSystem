"""
Process entry point - same startup sequence/style as the reference
project's main.py: log bootstrap -> monitor heartbeat -> broker session
(retry until connected) -> WebSocket feed -> wait for the ATM lock time ->
setup_straddle() (locks strike, subscribes feed, starts manager.py).
"""
import log_setup
log_setup.init()   # mirror all print()/stderr output to logs/<date>/app.log

import sys
from pathlib import Path

# trading_agent.py's START_ALGO only marks this process RUNNING once it
# sees a self-written PID file (write_pid_file) -- without it, it always
# times out after 2s and reports ERROR ("process exited immediately
# after launch") no matter how healthy the actual process is. This algo
# came from its own separate repo (not the trading/algos/example_strategy
# template every other algo here is copied from), so it never had this
# control-framework contract wired in. MUST run before `import monitor`
# below (not after) -- monitor.py's own `from trading.common.heartbeat
# import ...` needs project_root on sys.path at IMPORT time, not just by
# the time monitor.start() is later called; getting this backwards was a
# real bug caught live: the import silently failed (caught by monitor.py's
# own except ImportError), so config-center heartbeat reporting never
# worked, elsewhere no matter how monitor.start() as called.
_project_root = Path(__file__).resolve().parents[3]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
from trading.common.utils import write_pid_file  # noqa: E402
write_pid_file("CombinedVwapNifty")

import warnings, threading, token_file, connectapi, config, Websocket, rest_func, time, monitor
from datetime import datetime
warnings.filterwarnings('ignore')

# Start the Central Strategy Monitoring heartbeat agent (daemon thread).
monitor.start()   # -> status "RUNNING"

# Announce the run mode loudly so it's obvious in the logs whether real
# orders will be placed.
if config.DRY_RUN:
    print('[RUN MODE] DRY_RUN=True -> PAPER TRADING (no real orders will be placed)')
else:
    print('[RUN MODE] DRY_RUN=False -> LIVE TRADING (REAL orders will be placed!)')
token_file.download_token()
config.objconn = connectapi.makeconnection()
threading.Thread(target=Websocket.ConnectSocket).start()
while(True):
    dt = datetime.now()
    #Always use minutes as odd number,don't change second   #Attention#
    if(dt.hour==9 and dt.minute==30 and dt.second==00):
        rest_func.setup_straddle()
        break
    time.sleep(0.1)
