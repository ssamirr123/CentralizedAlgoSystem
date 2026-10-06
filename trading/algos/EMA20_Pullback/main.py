"""
Entry point: python trading/algos/EMA20_Pullback/main.py

Long-running paper process. Inside the IST market window the loop ticks
every `EMA20_PULLBACK_LOOP_INTERVAL` seconds (default 15 s):

  1. Fetch today's NIFTY 1-min spot bars.
  2. If an open leg exists: mark-to-market and evaluate exits
     (SL / Target1-partial-book / Trail-after-booking / Hard 14:30 stop).
  3. If flat and still eligible (max 2 losses/day, no weekly halt, not
     a Thursday expiry): scan for a new EMA20-pullback trigger.
  4. Persist the ledger and push heartbeat / trade reports.

PAPER ONLY. Fills simulated at option LTP returned by OptionQuoteService
at trigger/exit time. Ledger in `trading/data/EMA20_Pullback.portfolio.json`
survives restarts.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[3]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from trading.algos.EMA20_Pullback.config import (
    ALGO_NAME, HARD_EXIT_TIME, MAX_LOSSES_PER_DAY, SIGNAL, SKIP_EXPIRY_DAY,
    SL_PCT, TARGET1_PCT, WEEKLY_DD_HALT_PCT,
    EmaPullbackConfig, load_strategy_config,
)
from trading.algos.EMA20_Pullback.market_data import (
    OptionQuoteService, fetch_spot_minutes, load_expiry_calendar,
    next_weekly_expiry, thursday_of_week,
)
from trading.algos.EMA20_Pullback.portfolio import OpenLeg, Portfolio
from trading.algos.EMA20_Pullback.strategy import (
    ExitDecision, SignalDecision, detect, evaluate_exit, is_tradable_day,
)
from trading.common.config import TradingConfig, load_config
from trading.common.heartbeat import ControlCenterHeartbeatAgent
from trading.common.log_shipper import LogShipper
from trading.common.logger import attach_shipper, get_logger, log_event
from trading.common.reporting import report_daily_pnl, report_position, report_trade
from trading.common.utils import (
    GracefulShutdown, clear_stop_flag, get_hostname, get_pid, remove_pid_file, write_pid_file,
)

IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _in_window(now: datetime, open_t: dtime, close_t: dtime) -> bool:
    return now.weekday() < 5 and open_t <= now.time() <= close_t


class EmaPullbackRunner:
    def __init__(self, config: TradingConfig, strat: EmaPullbackConfig, server_name: str,
                 logger: logging.Logger, cc_agent: ControlCenterHeartbeatAgent | None) -> None:
        self._config = config
        self._strat = strat
        self._server = server_name
        self._logger = logger
        self._cc = cc_agent
        self.portfolio = Portfolio.load_or_new(strat.state_file, strat.initial_capital)
        self._option_svc = OptionQuoteService()
        self._expiries = load_expiry_calendar()
        self._last_heartbeat = datetime.min

    def _report(self, fn, **kwargs) -> None:
        if self._cc is None:
            return
        try:
            fn(self._config.api_base_url, self._config.control_api_key,
               self._config.strategy_name, self._server, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log_event(self._logger, logging.WARNING, "REPORT_FAILED", error=str(exc))

    def push_metrics(self, status: str = "RUNNING", mark_price: float | None = None) -> None:
        if self._cc is None:
            return
        p = self.portfolio
        equity = p.total_value(mark_price)
        summary = f"{p.open_leg.direction}@{p.open_leg.strike}" if p.open_leg else "flat"
        self._cc.update_metrics(
            status=status,
            pnl=round(p.day_pnl(), 2),
            position=summary,
            trading_mode="PAPER",
            running_lots=1 if p.open_leg else 0,
            equity=round(equity, 2),
        )

    def on_start(self) -> None:
        p = self.portfolio
        if p.initial_capital != self._strat.initial_capital and self._strat.state_file.exists():
            log_event(self._logger, logging.INFO, "CAPITAL_FROM_LEDGER",
                      ledger=p.initial_capital, env=self._strat.initial_capital)
        log_event(self._logger, logging.INFO, "PORTFOLIO_LOADED",
                  cash=round(p.cash, 2),
                  open_leg=p.open_leg.direction + str(p.open_leg.strike) if p.open_leg else None,
                  closed_trades=len(p.closed_trades))
        self.push_metrics()

    def on_stop(self) -> None:
        self.portfolio.save(self._strat.state_file)

    def on_tick(self, now: datetime) -> None:
        strat = self._strat
        if not _in_window(now, strat.market_open, strat.market_close):
            self._maybe_heartbeat(now, None)
            return

        today = now.date()
        spot = fetch_spot_minutes(today)
        if spot.empty:
            self._maybe_heartbeat(now, None)
            return

        mark = self._mark_leg(today)
        equity = self.portfolio.total_value(mark)
        self.portfolio.roll_session(today, equity)
        wdd = self.portfolio.weekly_dd_pct(equity)
        if wdd <= -WEEKLY_DD_HALT_PCT and not self.portfolio.is_halted():
            self.portfolio.halt_this_week()
            log_event(self._logger, logging.WARNING, "WEEKLY_HALT",
                      weekly_dd_pct=round(wdd * 100, 2))

        # Exits.
        if self.portfolio.open_leg is not None and mark is not None:
            leg = self.portfolio.open_leg
            if not leg.booked_half and mark >= leg.target1_price:
                proceeds = self.portfolio.book_half(leg.target1_price)
                log_event(self._logger, logging.INFO, "BOOK_HALF",
                          direction=leg.direction, strike=leg.strike,
                          price=leg.target1_price, proceeds=round(proceeds, 2))
            decision = evaluate_exit(leg, mark, spot, now)
            if decision is not None:
                self._close_leg(decision, now)
            mark = self._mark_leg(today)

        # Entry eligibility.
        if now.time() >= HARD_EXIT_TIME:
            self._maybe_heartbeat(now, mark)
            return
        if self.portfolio.open_leg is not None:
            self._maybe_heartbeat(now, mark)
            return
        if self.portfolio.is_halted():
            self._maybe_heartbeat(now, mark)
            return
        if not is_tradable_day(today, self._expiries, SKIP_EXPIRY_DAY):
            self._maybe_heartbeat(now, mark)
            return
        if self.portfolio.losses_today >= MAX_LOSSES_PER_DAY:
            self._maybe_heartbeat(now, mark)
            return

        proposal = detect(spot, now)
        if proposal is not None:
            self._open_leg(proposal, today, now)

        self._maybe_heartbeat(now, self._mark_leg(today))

    def _mark_leg(self, today: date) -> float | None:
        leg = self.portfolio.open_leg
        if leg is None:
            return None
        return self._option_svc.ltp(date.fromisoformat(leg.expiry), leg.strike, leg.direction)

    def _open_leg(self, proposal: SignalDecision, today: date, now: datetime) -> None:
        expiry = next_weekly_expiry(today, self._expiries) or thursday_of_week(today)
        if (expiry - today).days > 7:
            return
        ltp = self._option_svc.ltp(expiry, proposal.strike, proposal.direction)
        if ltp is None or ltp <= 1 or ltp > 2000:
            log_event(self._logger, logging.WARNING, "ENTRY_SKIPPED_NO_QUOTE",
                      direction=proposal.direction, strike=proposal.strike)
            return
        qty = self._strat.lot_size * self._strat.lots_per_trade
        cost = qty * ltp
        if cost > self.portfolio.cash:
            log_event(self._logger, logging.WARNING, "ENTRY_SKIPPED_INSUFFICIENT_CASH",
                      cost=round(cost, 2), cash=round(self.portfolio.cash, 2))
            return

        sl_px = ltp * (1 - SL_PCT)
        t1_px = ltp * (1 + TARGET1_PCT)
        leg = OpenLeg(
            direction=proposal.direction, strike=proposal.strike,
            expiry=expiry.isoformat(), qty=qty, entry_price=ltp,
            entry_dt=now.isoformat(timespec="seconds"), spot_at_entry=proposal.spot,
            sl_price=sl_px, target1_price=t1_px,
        )
        self.portfolio.open(leg)
        self.portfolio.save(self._strat.state_file)

        order_id = f"PAPER-{now:%Y%m%d-%H%M}-{SIGNAL}-{proposal.direction}-{proposal.strike}"
        log_event(self._logger, logging.INFO, "ENTRY",
                  direction=proposal.direction, strike=proposal.strike, expiry=leg.expiry,
                  qty=qty, price=round(ltp, 2), sl=round(sl_px, 2), target1=round(t1_px, 2),
                  spot=round(proposal.spot, 2), mode="PAPER")
        self._report(report_trade,
                     symbol=f"NIFTY-{proposal.strike}-{proposal.direction}",
                     side="BUY", quantity=qty, price=ltp, order_id=order_id)

    def _close_leg(self, decision: ExitDecision, now: datetime) -> None:
        trade = self.portfolio.close(
            decision.price, now.isoformat(timespec="seconds"), decision.reason,
            self._strat.brokerage_per_leg, self._strat.cost_turnover_pct,
        )
        self.portfolio.save(self._strat.state_file)
        order_id = f"PAPER-{now:%Y%m%d-%H%M}-{SIGNAL}-EXIT"
        log_event(self._logger, logging.INFO, "EXIT",
                  reason=decision.reason,
                  entry_price=round(trade["entry_price"], 2),
                  exit_price=round(trade["exit_price"], 2),
                  pnl_inr=trade["pnl_inr"], pnl_pct=trade["pnl_pct"], mode="PAPER")
        self._report(report_trade,
                     symbol=f"NIFTY-{trade['strike']}-{trade['direction']}",
                     side="SELL", quantity=trade["qty"], price=decision.price, order_id=order_id)

    def _maybe_heartbeat(self, now: datetime, mark: float | None) -> None:
        push_every = timedelta(minutes=self._strat.heartbeat_push_minutes)
        if now - self._last_heartbeat < push_every:
            return
        self._last_heartbeat = now
        leg = self.portfolio.open_leg
        if leg is not None and mark is not None:
            unreal = (mark - leg.entry_price) * leg.qty
            self._report(report_position,
                         symbol=f"NIFTY-{leg.strike}-{leg.direction}",
                         quantity=leg.qty, average_price=leg.entry_price,
                         last_price=mark, pnl=round(unreal, 2))
        today_iso = now.date().isoformat()
        self._report(report_daily_pnl,
                     pnl=round(self.portfolio.day_pnl(), 2),
                     trade_count=sum(1 for t in self.portfolio.closed_trades
                                     if t["exit_date"] == today_iso))
        self.push_metrics(mark_price=mark)


def main() -> int:
    config = load_config()
    strat = load_strategy_config()
    server_name = config.server_name or get_hostname()
    logger = get_logger(component=ALGO_NAME, server=server_name, algo=ALGO_NAME)

    log_event(logger, logging.INFO, "ALGO_STARTED", pid=get_pid(), mode=strat.mode,
              capital=strat.initial_capital, lots=strat.lots_per_trade,
              state_file=str(strat.state_file))
    if strat.mode != "paper":
        log_event(logger, logging.ERROR, "ERROR",
                  message=f"EMA20_PULLBACK_MODE={strat.mode!r} is not supported; only 'paper' is implemented")
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
    runner = EmaPullbackRunner(config, strat, server_name, logger, cc_agent)
    exit_code = 0
    try:
        runner.on_start()
        log_event(logger, logging.INFO, "START", mode="PAPER")
        while not shutdown.is_set():
            try:
                runner.on_tick(_now_ist())
            except Exception as exc:  # noqa: BLE001
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
