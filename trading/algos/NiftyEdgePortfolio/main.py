"""
NiftyEdgePortfolio entry point - same startup contract as DoubleStraddelAlgo:
log tee -> PID file -> trading-day check -> scrip master -> login (retry) ->
websocket thread -> engine loop. DRY_RUN (paper) unless NEP_LIVE=true.

Start it before 09:15 on each trading day (scheduler / watchdog); it stops by
itself after 15:31 and resumes overnight positions from runtime/state.json.
"""
import log_setup
log_setup.init()

import sys
import threading
import time
import warnings
from datetime import date
from pathlib import Path

warnings.filterwarnings("ignore")
_ALGO_DIR = Path(__file__).resolve().parent
_ROOT = Path(__file__).resolve().parents[3]
for p in (str(_ROOT), str(_ALGO_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import config  # noqa: E402
from trading.common.utils import write_pid_file  # noqa: E402

write_pid_file(config.ALGO_NAME)

import connectapi  # noqa: E402
import engine  # noqa: E402
import feed  # noqa: E402
import instruments  # noqa: E402
import monitor  # noqa: E402


def main():
    today = date.today()
    if not instruments.is_trading_day(today):
        from trading.common.market_calendar import non_trading_reason
        print(f"[STARTUP] Not a trading day ({non_trading_reason(today) or 'holiday list'}) - exiting without touching the broker.")
        return 0
    print(f"[STARTUP] {config.ALGO_NAME} | DRY_RUN={config.DRY_RUN} | strategies="
          f"{[(n, c['lots']) for n, c in config.STRATEGIES.items() if c['enabled']]}")
    monitor.start()
    instruments.load()
    config.objconn = connectapi.makeconnection()
    while config.objconn is None:
        print("[STARTUP] no broker session yet - retrying in 5s")
        time.sleep(5)
        config.objconn = connectapi.makeconnection()
    threading.Thread(target=feed.connect, daemon=True).start()
    time.sleep(3)
    ctx = engine.run()
    monitor.stop("STOPPED", ctx)
    print("[STARTUP] SHUTTING DOWN - engine finished for the day")
    return 0


if __name__ == "__main__":
    sys.exit(main())
