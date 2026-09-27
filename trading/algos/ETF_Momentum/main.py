"""
Entry point: python trading/algos/ETF_Momentum/main.py

Long-running process (Start/Stop from the Control Center like any other
algo). Most of the month it only refreshes marks and sends heartbeats; on
the first trading day of each month, after the close, it rebalances the
paper portfolio into the top-6 ETFs by momentum -- the same rules as the
backtest ETF_Momentum_Test_1.py.

PAPER ONLY: no broker is connected and no real order is ever placed.
Fills are simulated at the rebalance day's close and kept in
trading/data/ETF_Momentum.portfolio.json, which survives restarts.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[3]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

import pandas as pd

from trading.algos.ETF_Momentum.config import (
    ALGO_NAME, UNIVERSE, EtfMomentumConfig, load_strategy_config,
)
from trading.algos.ETF_Momentum.portfolio import Portfolio
from trading.algos.ETF_Momentum.strategy import is_first_trading_day_of_month, rebalance
from trading.common.config import TradingConfig, load_config
from trading.common.heartbeat import ControlCenterHeartbeatAgent
from trading.common.log_shipper import LogShipper
from trading.common.logger import attach_shipper, get_logger, log_event
from trading.common.reporting import report_daily_pnl, report_position, report_trade
from trading.common.utils import (
    GracefulShutdown, clear_stop_flag, get_hostname, get_pid, remove_pid_file, write_pid_file,
)

IST = ZoneInfo("Asia/Kolkata")
MARKET_WINDOW = (dtime(9, 0), dtime(16, 30))   # IST, marks refreshed every N min inside it


def _at(day: date, t: dtime) -> datetime:
    return datetime.combine(day, t, tzinfo=IST)


class EtfMomentumRunner:
    def __init__(self, config: TradingConfig, strat: EtfMomentumConfig, server_name: str,
                 logger: logging.Logger, cc_agent: ControlCenterHeartbeatAgent | None) -> None:
        self._config = config
        self._strat = strat
        self._server = server_name
        self._logger = logger
        self._cc = cc_agent
        self.portfolio = Portfolio.load_or_new(strat.state_file, strat.initial_capital)
        self._next_rebalance_check = datetime.min.replace(tzinfo=IST)
        self._next_price_refresh = datetime.min.replace(tzinfo=IST)
        self._attempts = (None, 0)          # (date, rebalance attempts that day)

    # ── reporting helpers (never raise; dashboard failures never block trading) ──
    @property
    def _reporting(self) -> bool:
        return self._cc is not None

    def _report(self, fn, **kwargs) -> None:
        if self._reporting:
            fn(self._config.api_base_url, self._config.control_api_key, self._config.strategy_name,
               self._server, **kwargs)

    def _trades_on(self, day: date) -> int:
        iso = day.isoformat()
        exits = sum(1 for t in self.portfolio.closed_trades if t["exit_date"] == iso)
        entries = sum(1 for h in self.portfolio.holdings.values() if h.entry_date == iso)
        entries += sum(1 for t in self.portfolio.closed_trades if t["entry_date"] == iso)
        return exits + entries

    def push_metrics(self, status: str = "RUNNING") -> None:
        if self._cc is None:
            return
        p = self.portfolio
        summary = f"{len(p.holdings)} ETFs | value {p.total_value():,.0f} | cash {p.cash:,.0f}"
        self._cc.update_metrics(status=status, pnl=round(p.day_pnl(), 2), position=summary,
                                trading_mode="PAPER", running_lots=len(p.holdings))

    # ── main-loop tick ───────────────────────────────────────────────────────
    def on_start(self) -> None:
        p = self.portfolio
        if p.initial_capital != self._strat.initial_capital and self._strat.state_file.exists():
            log_event(self._logger, logging.INFO, "CAPITAL_FROM_LEDGER",
                      ledger=p.initial_capital, env=self._strat.initial_capital)
        log_event(self._logger, logging.INFO, "PORTFOLIO_LOADED", holdings=sorted(p.holdings),
                  cash=round(p.cash, 2), value=round(p.total_value(), 2), last_rebalance=p.last_rebalance)
        self.push_metrics()

    def on_tick(self, now: datetime) -> None:
        if self._rebalance_due(now):
            self._try_rebalance(now)
        if now >= self._next_price_refresh:
            self._refresh_prices(now)
        self.push_metrics()

    # ── rebalance ────────────────────────────────────────────────────────────
    def _rebalance_due(self, now: datetime) -> bool:
        today = now.date()
        if now.weekday() >= 5 or now.time() < self._strat.rebalance_time:
            return False
        if now < self._next_rebalance_check:
            return False
        last = self.portfolio.last_rebalance
        if last is not None and date.fromisoformat(last).replace(day=1) == today.replace(day=1):
            return False                     # already rebalanced this month
        if last is None and not self._strat.start_now and today.day > 7:
            return False                     # fresh start: wait for next month's first trading day
        return True

    def _schedule_next_check(self, now: datetime, tomorrow: bool) -> None:
        if tomorrow:
            self._next_rebalance_check = _at(now.date() + timedelta(days=1), self._strat.rebalance_time)
        else:
            self._next_rebalance_check = now + timedelta(minutes=self._strat.rebalance_retry_minutes)

    def _try_rebalance(self, now: datetime) -> None:
        from trading.algos.ETF_Momentum.market_data import fetch_closes

        today = now.date()
        day, n = self._attempts
        self._attempts = (today, (n + 1) if day == today else 1)

        symbols = [s for s, _ in UNIVERSE]
        closes, missing = fetch_closes(symbols, today - timedelta(days=self._strat.history_calendar_days), today)
        if closes.empty or pd.Timestamp(today) not in closes.index:
            give_up = self._attempts[1] >= self._strat.rebalance_max_attempts_per_day
            log_event(self._logger, logging.INFO, "NO_CLOSE_FOR_TODAY",
                      attempt=self._attempts[1], giving_up_for_today=give_up)
            self._schedule_next_check(now, tomorrow=give_up)
            return

        p = self.portfolio
        if is_first_trading_day_of_month(closes.index, today):
            kind = "SCHEDULED"
        elif p.last_rebalance is not None:
            kind = "CATCH_UP"
            log_event(self._logger, logging.WARNING, "WARNING",
                      message="First trading day of the month was missed; rebalancing late at today's close",
                      last_rebalance=p.last_rebalance)
        elif self._strat.start_now:
            kind = "START_NOW"
        else:
            log_event(self._logger, logging.INFO, "WAITING_FOR_FIRST_TRADING_DAY")
            self._schedule_next_check(now, tomorrow=True)
            return

        if missing:
            log_event(self._logger, logging.INFO, "SYMBOLS_WITHOUT_DATA", symbols=missing)

        result = rebalance(p, closes, today)
        prev_row = closes.iloc[-2] if len(closes) > 1 else closes.iloc[-1]
        for sym, h in p.holdings.items():
            h.prev_close = h.entry_price if h.entry_date == today.isoformat() else float(prev_row.get(sym, h.last_price))
        p.save(self._strat.state_file)
        self._schedule_next_check(now, tomorrow=True)

        for o in result.orders:
            order_id = f"PAPER-{today:%Y%m%d}-{o.side}-{o.symbol}"
            if o.side == "SELL":
                event = "SL" if o.reason == "SL" else "EXIT"
                log_event(self._logger, logging.INFO, event, symbol=o.symbol, qty=o.qty, price=o.price,
                          reason=o.reason, pnl_inr=round(o.pnl_inr or 0.0, 2), mode="PAPER")
                self._report(report_position, symbol=o.symbol, quantity=0, average_price=0.0)
            else:
                log_event(self._logger, logging.INFO, "ENTRY", symbol=o.symbol, qty=o.qty, price=o.price,
                          score=round(o.score or 0.0, 2), mode="PAPER")
            self._report(report_trade, symbol=o.symbol, side=o.side, quantity=o.qty, price=o.price,
                         order_id=order_id)
        log_event(self._logger, logging.INFO, "REBALANCE_DONE", kind=kind, as_of=today.isoformat(),
                  top=result.top, skipped_below_dma=result.skipped_below_dma,
                  orders=len(result.orders), interest=round(result.interest, 2),
                  value=round(result.value_after, 2), cash=round(p.cash, 2))
        self._next_price_refresh = now          # push fresh positions/P&L right away

    # ── marks between rebalances ─────────────────────────────────────────────
    def _refresh_prices(self, now: datetime) -> None:
        in_window = now.weekday() < 5 and MARKET_WINDOW[0] <= now.time() <= MARKET_WINDOW[1]
        step = timedelta(minutes=self._strat.price_refresh_minutes) if in_window else timedelta(hours=2)
        self._next_price_refresh = now + step

        p = self.portfolio
        if p.holdings:
            from trading.algos.ETF_Momentum.market_data import fetch_closes

            today = now.date()
            closes, _ = fetch_closes(sorted(p.holdings), today - timedelta(days=10), today)
            for sym, h in p.holdings.items():
                if closes.empty or sym not in closes:
                    continue
                s = closes[sym].dropna()
                if s.empty:
                    continue
                h.last_price = float(s.iloc[-1])
                traded_today = s.index[-1] == pd.Timestamp(today)
                if h.entry_date == today.isoformat():
                    h.prev_close = h.entry_price
                elif traded_today and len(s) > 1:
                    h.prev_close = float(s.iloc[-2])
                else:
                    h.prev_close = h.last_price    # no session today -> day P&L 0
            p.save(self._strat.state_file)

        for h in p.holdings.values():
            self._report(report_position, symbol=h.symbol, quantity=h.qty, average_price=h.entry_price,
                         last_price=h.last_price, pnl=round(h.unrealised_pnl, 2))
        self._report(report_daily_pnl, pnl=round(p.day_pnl(), 2), trade_count=self._trades_on(now.date()))

    def on_stop(self) -> None:
        self.portfolio.save(self._strat.state_file)


def main() -> int:
    config = load_config()
    strat = load_strategy_config()
    server_name = config.server_name or get_hostname()
    logger = get_logger(component=ALGO_NAME, server=server_name, algo=ALGO_NAME)

    log_event(logger, logging.INFO, "ALGO_STARTED", pid=get_pid(), mode=strat.mode,
              rebalance_time=strat.rebalance_time.isoformat(), state_file=str(strat.state_file))
    if strat.mode != "paper":
        log_event(logger, logging.ERROR, "ERROR",
                  message=f"ETF_MOMENTUM_MODE={strat.mode!r} is not supported; only 'paper' is implemented")
        return 1

    write_pid_file(ALGO_NAME)
    clear_stop_flag(ALGO_NAME)

    shipper: LogShipper | None = None
    cc_agent: ControlCenterHeartbeatAgent | None = None
    if config.control_api_key:
        shipper = LogShipper(algo_name=config.strategy_name, server_name=server_name,
                             api_base_url=config.api_base_url, api_key=config.control_api_key)
        shipper.start()
        attach_shipper(logger, shipper)
        cc_agent = ControlCenterHeartbeatAgent(
            algo_name=config.strategy_name, server_name=server_name, api_base_url=config.api_base_url,
            api_key=config.control_api_key, interval_seconds=config.control_heartbeat_interval_seconds,
        )
        cc_agent.start()
    else:
        log_event(logger, logging.INFO, "CONTROL_CENTER_HEARTBEAT_SKIPPED", reason="CONTROL_API_KEY not set")

    shutdown = GracefulShutdown(algo_name=ALGO_NAME)
    runner = EtfMomentumRunner(config, strat, server_name, logger, cc_agent)
    exit_code = 0
    try:
        runner.on_start()
        log_event(logger, logging.INFO, "START", mode="PAPER")
        while not shutdown.is_set():
            try:
                runner.on_tick(datetime.now(IST))
            except Exception as exc:  # noqa: BLE001
                # One bad tick (e.g. a yfinance hiccup) must not kill a months-long run.
                log_event(logger, logging.ERROR, "TICK_ERROR", error=str(exc))
                if cc_agent is not None:
                    runner.push_metrics(status="ERROR")
            shutdown.wait(strat.loop_interval_seconds)
    except Exception as exc:  # noqa: BLE001
        log_event(logger, logging.CRITICAL, "FATAL_ERROR", error=str(exc))
        exit_code = 1
    finally:
        try:
            runner.on_stop()
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "STRATEGY_STOP_ERROR", error=str(exc))
        if cc_agent is not None:
            runner.push_metrics(status="STOPPED")
            cc_agent.stop()
        log_event(logger, logging.INFO, "STOP")
        if shipper:
            shipper.stop()
        remove_pid_file(ALGO_NAME)
        clear_stop_flag(ALGO_NAME)
        log_event(logger, logging.INFO, "ALGO_STOPPED_SAFELY")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
