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
# the time monitor.start() is later called (a real bug caught live on
# CombinedVwapNifty: getting this backwards silently broke the
# control-center heartbeat for its entire process lifetime).
_project_root = Path(__file__).resolve().parents[3]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
from trading.common.utils import write_pid_file  # noqa: E402
write_pid_file("Vwap_Algo_Nifty_hedge")

# Local modules imported AFTER _project_root is on sys.path -- monitor.py
# does `from trading.common.heartbeat import ...` at import time, and if
# that fails (repo root not yet on the path) it silently disables the
# control-center heartbeat for the whole process. Matches CombinedVwapNifty.
import monitor, warnings, threading, token_file, connectapi, config, Websocket, rest_func, time
from datetime import datetime

warnings.filterwarnings('ignore')

# Start the Central Strategy Monitoring heartbeat agent (daemon thread).
# This is fully best-effort and must never affect the strategy run.
monitor.start()   # -> status "RUNNING"

token_file.download_token()
config.objconn = connectapi.makeconnection()
threading.Thread(target=Websocket.ConnectSocket).start()
while(True):
    dt = datetime.now()
    #Always use minutes as odd number,don't change second   #Attention#
    if(dt.hour==9 and dt.minute==17 and dt.second==2):
        rest_func.add_make_option()
        break
    time.sleep(0.1)
