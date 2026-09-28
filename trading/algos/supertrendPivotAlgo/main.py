"""
main.py
Entry point: wires up login, data, strategy and scheduler, then runs the bot.
"""

import sys
from pathlib import Path

# Repo root on sys.path so trading.common (PID file, stop flag, dashboard
# heartbeat) is importable. Appended, not prepended, so this folder's own
# config/logger/strategy modules always win over anything at the repo root.
_project_root = Path(__file__).resolve().parents[3]
if str(_project_root) not in sys.path:
    sys.path.append(str(_project_root))

import config
from trading.common.utils import (  # noqa: E402
    GracefulShutdown, clear_stop_flag, remove_pid_file, write_pid_file,
)

# The dashboard's Start command only marks the strategy RUNNING once this
# process writes its own PID file; do it before anything slow (login etc.).
write_pid_file(config.ALGO_NAME)
clear_stop_flag(config.ALGO_NAME)

from logger import get_logger
from login import login
from market_data import MarketData
from option_chain import OptionChain
from order_manager import OrderManager
from strategy import Strategy
from scheduler import BotScheduler
from tick_stream import LiveCandleStream
from monitor import ControlCenterMonitor

log = get_logger()


def _build_agent(order_manager):
    """Start dashboard monitoring. Returns None (never raises) if it is
    disabled or not configured, so the strategy always runs regardless."""
    monitor = ControlCenterMonitor(order_manager)
    return monitor if monitor.start() else None


def _report(agent, order_manager, status):
    """Best-effort metric push; never blocks or raises into the trading loop."""
    if agent is None:
        return
    try:
        agent.update_metrics(
            order_manager.mtm(),
            order_manager.realized_pnl,
            order_manager.trades_today,
            status,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("Status report (%s) failed: %s", status, exc)


def main():
    log.info("=" * 60)
    log.info("Starting Option Selling Bot | index=%s | dry_run=%s",
             config.INDEX, config.DRY_RUN)
    log.info("Max trades/day=%d | product=%s | qty_lots=%d",
             config.MAX_TRADES_PER_DAY, config.PRODUCT_TYPE,
             config.QUANTITY_LOTS)
    log.info("=" * 60)

    smart = login()

    market_data = MarketData(smart)
    option_chain = OptionChain()
    order_manager = OrderManager(smart)
    strategy = Strategy(market_data, option_chain, order_manager)

    # Start the live tick -> 5-minute candle stream, if enabled.
    stream = None
    if config.USE_WEBSOCKET:
        stream = LiveCandleStream(smart)
        if stream.start():
            market_data.attach_stream(stream)
        else:
            log.warning("Live stream unavailable; falling back to REST candles.")

    # Dashboard Stop writes a stop-flag file; SIGTERM/Ctrl+C also set it.
    shutdown = GracefulShutdown(algo_name=config.ALGO_NAME)
    scheduler = BotScheduler(strategy, order_manager, shutdown=shutdown)

    # Dashboard heartbeat / P&L / positions (daemon threads, non-blocking).
    agent = _build_agent(order_manager)
    order_manager.attach_agent(agent)
    # Strategy is now live.
    _report(agent, order_manager, "RUNNING")

    try:
        scheduler.run()
        # Reached the end of the trading session cleanly.
        _report(agent, order_manager, "STOPPED")
    except KeyboardInterrupt:
        log.warning("Interrupted by user. Squaring off before exit...")
        order_manager.square_off_all()
        _report(agent, order_manager, "STOPPED")
    except Exception:
        # Any unhandled error -> mark ERROR, then re-raise so it isn't hidden.
        log.exception("Unhandled exception in trading loop.")
        _report(agent, order_manager, "ERROR")
        raise
    finally:
        if stream is not None:
            stream.stop()
        if agent is not None:
            agent.stop()
        remove_pid_file(config.ALGO_NAME)
        log.info("Bot shutdown. Trades taken: %d | Realized P&L: %.2f",
                 order_manager.trades_today, order_manager.realized_pnl)


if __name__ == "__main__":
    main()

