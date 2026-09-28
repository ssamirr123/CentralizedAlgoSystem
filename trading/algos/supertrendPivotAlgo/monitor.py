"""
monitor.py
Connects this strategy to the control-center dashboard.

Drop-in replacement for the old StrategyHeartbeatAgent (which posted to the
removed /update_strategy endpoint): same update_metrics(mtm, pnl, trades,
status) call the OrderManager already makes, but reported through
trading.common's ControlCenterHeartbeatAgent (POST /api/heartbeat) plus the
dashboard's P&L and Positions feeds.

Best-effort throughout: nothing here may block or raise into the trading loop.
"""

import threading
import time

import config
from logger import get_logger

log = get_logger()

try:
    from trading.common.heartbeat import ControlCenterHeartbeatAgent
    from trading.common.reporting import report_daily_pnl, report_position
except Exception as _exc:  # noqa: BLE001 - repo root not importable (manual run elsewhere)
    ControlCenterHeartbeatAgent = None
    report_daily_pnl = report_position = None
    log.warning("Control-center modules unavailable (%s); dashboard reporting disabled.", _exc)

_PNL_REPORT_INTERVAL_SECONDS = 60


class ControlCenterMonitor:
    def __init__(self, order_manager):
        self.om = order_manager
        self._agent = None
        self._stop = threading.Event()
        self._last_pnl_report = 0.0
        self._reported_symbol = None     # symbol currently shown on the Positions tab

    # ------------------------------------------------------------- lifecycle
    def start(self):
        """Start heartbeats. Returns False (and stays a no-op) if not configured."""
        if not config.MONITOR_ENABLED:
            log.info("Dashboard monitoring disabled (MONITOR_ENABLED=false).")
            return False
        if ControlCenterHeartbeatAgent is None:
            return False
        if not (config.SERVER_NAME and config.API_BASE_URL and config.CONTROL_API_KEY):
            log.info("Dashboard heartbeat skipped: SERVER_NAME/API_BASE_URL/CONTROL_API_KEY "
                     "not set (not launched from the dashboard?).")
            return False
        try:
            self._agent = ControlCenterHeartbeatAgent(
                algo_name=config.STRATEGY_NAME,
                server_name=config.SERVER_NAME,
                api_base_url=config.API_BASE_URL,
                api_key=config.CONTROL_API_KEY,
                interval_seconds=config.MONITOR_HEARTBEAT_INTERVAL_SECONDS,
            )
            self._agent.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("Heartbeat agent unavailable, continuing without it: %s", exc)
            self._agent = None
            return False
        threading.Thread(target=self._refresh_loop, name="dashboard-refresh", daemon=True).start()
        return True

    def stop(self):
        self._stop.set()
        if self._agent is not None:
            try:
                self._agent.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("Heartbeat agent stop failed: %s", exc)

    # ----------------------------------------------- same API as the old agent
    def update_metrics(self, mtm, pnl, trades, status="RUNNING"):
        """mtm = realized + unrealized day P&L; pnl = realized only."""
        if self._agent is None:
            return
        try:
            pos = self.om.current_position
            self._agent.update_metrics(
                status=status,
                pnl=round(float(mtm), 2),
                position=f"SHORT {pos.symbol}" if pos is not None else None,
                trading_mode="PAPER" if config.DRY_RUN else "LIVE",
                running_lots=config.QUANTITY_LOTS,
            )
            self._report_pnl(mtm, trades, force=status != "RUNNING")
            self._report_position(pos)
        except Exception as exc:  # noqa: BLE001
            log.debug("Dashboard report (%s) failed: %s", status, exc)

    # ------------------------------------------------------------- internals
    def _report_pnl(self, mtm, trades, force=False):
        if report_daily_pnl is None:
            return
        now = time.monotonic()
        if not force and now - self._last_pnl_report < _PNL_REPORT_INTERVAL_SECONDS:
            return
        self._last_pnl_report = now
        report_daily_pnl(config.API_BASE_URL, config.CONTROL_API_KEY, config.STRATEGY_NAME,
                         config.SERVER_NAME, pnl=round(float(mtm), 2), trade_count=int(trades))

    def _report_position(self, pos):
        """Show the open short on the Positions tab; quantity=0 removes a closed one."""
        if report_position is None:
            return
        if pos is None:
            if self._reported_symbol is not None:
                report_position(config.API_BASE_URL, config.CONTROL_API_KEY, config.STRATEGY_NAME,
                                config.SERVER_NAME, symbol=self._reported_symbol, quantity=0,
                                average_price=0.0)
                self._reported_symbol = None
            return
        if self._reported_symbol not in (None, pos.symbol):
            self._report_position(None)
        report_position(config.API_BASE_URL, config.CONTROL_API_KEY, config.STRATEGY_NAME,
                        config.SERVER_NAME, symbol=pos.symbol, quantity=-int(pos.qty),
                        average_price=float(pos.entry_price))
        self._reported_symbol = pos.symbol

    def _refresh_loop(self):
        """Keep dashboard P&L current between trades while a position is open
        (OrderManager.mtm() costs one ltpData call, so only then)."""
        while not self._stop.wait(config.MONITOR_REFRESH_SECONDS):
            if self.om.current_position is not None:
                self.om.report_metrics(status="RUNNING")
