"""
Control-center dashboard integration for EMA20_Pullback.

Mirrors `supertrendPivotAlgo.monitor.ControlCenterMonitor`: wraps the
shared `ControlCenterHeartbeatAgent` and dashboard report endpoints
with graceful degradation when monitoring is disabled or the dashboard
environment is missing. Best-effort — never blocks or raises into the
trading loop.
"""
from __future__ import annotations

import threading
import time

from trading.algos.EMA20_Pullback import config
from trading.algos.EMA20_Pullback.logger import get_logger

log = get_logger()

try:
    from trading.common.heartbeat import ControlCenterHeartbeatAgent
    from trading.common.reporting import report_daily_pnl, report_position
except Exception as _exc:  # noqa: BLE001
    ControlCenterHeartbeatAgent = None     # type: ignore[assignment]
    report_daily_pnl = report_position = None   # type: ignore[assignment]
    log.warning("Control-center modules unavailable (%s); dashboard reporting disabled.", _exc)

_PNL_REPORT_INTERVAL_SECONDS = 60


class ControlCenterMonitor:
    def __init__(self, portfolio) -> None:
        self.portfolio = portfolio
        self._agent = None
        self._stop = threading.Event()
        self._last_pnl_report = 0.0
        self._reported_symbol: str | None = None

    def start(self) -> bool:
        if not config.MONITOR_ENABLED:
            log.info("Dashboard monitoring disabled (MONITOR_ENABLED=false).")
            return False
        if ControlCenterHeartbeatAgent is None:
            return False
        if not (config.SERVER_NAME and config.API_BASE_URL and config.CONTROL_API_KEY):
            log.info("Dashboard heartbeat skipped: SERVER_NAME / API_BASE_URL / "
                     "CONTROL_API_KEY not set (not launched from the dashboard?).")
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
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._agent is not None:
            try:
                self._agent.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("Heartbeat agent stop failed: %s", exc)

    def update_metrics(self, mark_price: float | None, status: str = "RUNNING") -> None:
        if self._agent is None:
            return
        try:
            p = self.portfolio
            equity = p.total_value(mark_price)
            leg = p.open_leg
            position = f"{leg.direction}@{leg.strike}" if leg is not None else None
            self._agent.update_metrics(
                status=status,
                pnl=round(float(p.day_pnl()), 2),
                position=position,
                trading_mode="PAPER" if config.DRY_RUN else "LIVE",
                running_lots=config.QUANTITY_LOTS if leg is not None else 0,
                equity=round(float(equity), 2),
            )
            self._report_pnl(force=status != "RUNNING")
            self._report_position(mark_price)
        except Exception as exc:  # noqa: BLE001
            log.debug("Dashboard report (%s) failed: %s", status, exc)

    def _report_pnl(self, force: bool = False) -> None:
        if report_daily_pnl is None:
            return
        now = time.monotonic()
        if not force and now - self._last_pnl_report < _PNL_REPORT_INTERVAL_SECONDS:
            return
        self._last_pnl_report = now
        today_iso = config.today().isoformat()
        report_daily_pnl(
            config.API_BASE_URL, config.CONTROL_API_KEY, config.STRATEGY_NAME,
            config.SERVER_NAME,
            pnl=round(float(self.portfolio.day_pnl()), 2),
            trade_count=sum(1 for t in self.portfolio.closed_trades
                            if t["exit_date"] == today_iso),
        )

    def _report_position(self, mark_price: float | None) -> None:
        if report_position is None:
            return
        leg = self.portfolio.open_leg
        if leg is None:
            if self._reported_symbol is not None:
                report_position(
                    config.API_BASE_URL, config.CONTROL_API_KEY, config.STRATEGY_NAME,
                    config.SERVER_NAME, symbol=self._reported_symbol, quantity=0,
                    average_price=0.0,
                )
                self._reported_symbol = None
            return
        symbol = f"NIFTY-{leg.strike}-{leg.direction}"
        if self._reported_symbol not in (None, symbol):
            report_position(
                config.API_BASE_URL, config.CONTROL_API_KEY, config.STRATEGY_NAME,
                config.SERVER_NAME, symbol=self._reported_symbol, quantity=0,
                average_price=0.0,
            )
        unreal = ((mark_price if mark_price is not None else leg.entry_price) - leg.entry_price) * leg.qty
        report_position(
            config.API_BASE_URL, config.CONTROL_API_KEY, config.STRATEGY_NAME,
            config.SERVER_NAME, symbol=symbol, quantity=int(leg.qty),
            average_price=float(leg.entry_price),
            last_price=float(mark_price) if mark_price is not None else float(leg.entry_price),
            pnl=round(float(unreal), 2),
        )
        self._reported_symbol = symbol
