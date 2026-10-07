"""
main.py
Entry point: Angel One login → market data → strategy → scheduler →
dashboard monitor. Paper only — Angel LTP is used as the fill price;
no real order is placed.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_project_root = Path(__file__).resolve().parents[3]
if str(_project_root) not in sys.path:
    sys.path.append(str(_project_root))

from trading.algos.VWAP_Reclaim import config  # noqa: E402
from trading.common.utils import (  # noqa: E402
    GracefulShutdown, clear_stop_flag, remove_pid_file, write_pid_file,
)

# Mark RUNNING for the dashboard before any slow work.
write_pid_file(config.ALGO_NAME)
clear_stop_flag(config.ALGO_NAME)

from trading.algos.VWAP_Reclaim.logger import get_logger          # noqa: E402
from trading.algos.VWAP_Reclaim.login import login                # noqa: E402
from trading.algos.VWAP_Reclaim.market_data import MarketData     # noqa: E402
from trading.algos.VWAP_Reclaim.monitor import ControlCenterMonitor  # noqa: E402
from trading.algos.VWAP_Reclaim.option_chain import OptionChain   # noqa: E402
from trading.algos.VWAP_Reclaim.portfolio import OpenLeg, Portfolio   # noqa: E402
from trading.algos.VWAP_Reclaim.scheduler import Scheduler        # noqa: E402
from trading.algos.VWAP_Reclaim.strategy import (                 # noqa: E402
    ExitDecision, SignalDecision, detect, evaluate_exit, is_tradable_day,
)

log = get_logger()


class VwapReclaimBot:
    """Owns the tick: refresh spot, mark MTM, evaluate exits, scan entries."""

    def __init__(self, portfolio: Portfolio, market_data: MarketData,
                 monitor: ControlCenterMonitor | None) -> None:
        self.portfolio = portfolio
        self._md = market_data
        self._monitor = monitor
        self._last_heartbeat = datetime.min

    def tick(self, now: datetime) -> None:
        if not (now.weekday() < 5 and _in_window(now)):
            self._maybe_heartbeat(now, None)
            return

        today = now.date()
        spot = self._md.fetch_spot_minutes(today)
        if spot.empty:
            self._maybe_heartbeat(now, None)
            return

        mark = self._mark_leg()
        equity = self.portfolio.total_value(mark)
        self.portfolio.roll_session(today, equity)
        wdd = self.portfolio.weekly_dd_pct(equity)
        if wdd <= -config.WEEKLY_DD_HALT_PCT and not self.portfolio.is_halted():
            self.portfolio.halt_this_week()
            log.warning("WEEKLY_HALT weekly_dd_pct=%.2f%%", wdd * 100)

        # Exits (SL / Target1 book / Trail / TimeStop).
        if self.portfolio.open_leg is not None and mark is not None:
            leg = self.portfolio.open_leg
            if not leg.booked_half and mark >= leg.target1_price:
                proceeds = self.portfolio.book_half(leg.target1_price)
                log.info("BOOK_HALF direction=%s strike=%d price=%.2f proceeds=%.2f",
                         leg.direction, leg.strike, leg.target1_price, proceeds)
            decision = evaluate_exit(leg, mark, spot, now)
            if decision is not None:
                self._close(decision, now)
            mark = self._mark_leg()

        # Entry gates.
        if (now.time() >= config.HARD_EXIT_TIME
                or self.portfolio.open_leg is not None
                or self.portfolio.is_halted()
                or not is_tradable_day(today, [self._md.next_weekly_expiry()], config.SKIP_EXPIRY_DAY)
                or self.portfolio.losses_today >= config.MAX_LOSSES_PER_DAY):
            self._maybe_heartbeat(now, mark)
            return

        proposal = detect(spot, now)
        if proposal is not None:
            self._open(proposal, today, now)

        self._maybe_heartbeat(now, self._mark_leg())

    def _mark_leg(self) -> float | None:
        leg = self.portfolio.open_leg
        if leg is None:
            return None
        return self._md.option_ltp(leg.strike, leg.direction, date.fromisoformat(leg.expiry))

    def _open(self, proposal: SignalDecision, today: date, now: datetime) -> None:
        expiry = self._md.next_weekly_expiry()
        if (expiry - today).days > 7:
            return
        ltp = self._md.option_ltp(proposal.strike, proposal.direction, expiry)
        if ltp is None or ltp <= 1 or ltp > 2000:
            log.warning("ENTRY_SKIPPED_NO_QUOTE direction=%s strike=%d",
                        proposal.direction, proposal.strike)
            return
        qty = config.LOT_SIZE * config.QUANTITY_LOTS
        cost = qty * ltp
        if cost > self.portfolio.cash:
            log.warning("ENTRY_SKIPPED_INSUFFICIENT_CASH cost=%.2f cash=%.2f",
                        cost, self.portfolio.cash)
            return
        sl_px = ltp * (1 - config.SL_PCT)
        t1_px = ltp * (1 + config.TARGET1_PCT)
        leg = OpenLeg(
            direction=proposal.direction, strike=proposal.strike,
            expiry=expiry.isoformat(), qty=qty, entry_price=ltp,
            entry_dt=now.isoformat(timespec="seconds"), spot_at_entry=proposal.spot,
            sl_price=sl_px, target1_price=t1_px,
        )
        self.portfolio.open(leg)
        self.portfolio.save(config.STATE_FILE)
        log.info("ENTRY direction=%s strike=%d expiry=%s qty=%d price=%.2f sl=%.2f t1=%.2f spot=%.2f mode=%s",
                 proposal.direction, proposal.strike, leg.expiry, qty, ltp, sl_px, t1_px,
                 proposal.spot, "PAPER" if config.DRY_RUN else "LIVE")

    def _close(self, decision: ExitDecision, now: datetime) -> None:
        trade = self.portfolio.close(
            decision.price, now.isoformat(timespec="seconds"), decision.reason,
            config.BROKERAGE_PER_LEG, config.COST_TURNOVER_PCT,
        )
        self.portfolio.save(config.STATE_FILE)
        log.info("EXIT reason=%s entry=%.2f exit=%.2f pnl_inr=%.2f pnl_pct=%.2f mode=%s",
                 decision.reason, trade["entry_price"], trade["exit_price"],
                 trade["pnl_inr"], trade["pnl_pct"], "PAPER" if config.DRY_RUN else "LIVE")

    def _maybe_heartbeat(self, now: datetime, mark: float | None) -> None:
        if self._monitor is None:
            return
        if now - self._last_heartbeat < timedelta(seconds=config.MONITOR_REFRESH_SECONDS):
            return
        self._last_heartbeat = now
        self._monitor.update_metrics(mark)


def _in_window(now: datetime) -> bool:
    oh, om = map(int, config.MARKET_OPEN.split(":"))
    ch, cm = map(int, config.MARKET_CLOSE.split(":"))
    t = (now.time().hour, now.time().minute)
    return (oh, om) <= t <= (ch, cm)


def main() -> None:
    log.info("=" * 60)
    log.info("Starting VWAP_Reclaim | capital=%.0f | lots=%d | lot_size=%d | dry_run=%s",
             config.INITIAL_CAPITAL, config.QUANTITY_LOTS, config.LOT_SIZE, config.DRY_RUN)
    log.info("Window %s-%s | scan %s-%s | hard-exit %s | max_losses/day=%d",
             config.MARKET_OPEN, config.MARKET_CLOSE,
             config.SCAN_FROM.strftime("%H:%M"), config.SCAN_TO.strftime("%H:%M"),
             config.HARD_EXIT_TIME.strftime("%H:%M"), config.MAX_LOSSES_PER_DAY)
    log.info("=" * 60)

    broker = login()
    option_chain = OptionChain(broker)
    market_data = MarketData(broker, option_chain)
    log.info("Broker ready: %s", broker.kind)

    portfolio = Portfolio.load_or_new(config.STATE_FILE, config.INITIAL_CAPITAL)
    log.info("Portfolio loaded | cash=%.2f | open_leg=%s | closed=%d",
             portfolio.cash,
             f"{portfolio.open_leg.direction}@{portfolio.open_leg.strike}" if portfolio.open_leg else None,
             len(portfolio.closed_trades))

    shutdown = GracefulShutdown(algo_name=config.ALGO_NAME)
    monitor = ControlCenterMonitor(portfolio)
    agent = monitor if monitor.start() else None

    bot = VwapReclaimBot(portfolio, market_data, agent)

    if agent is not None:
        try:
            agent.update_metrics(None, status="RUNNING")
        except Exception as exc:  # noqa: BLE001
            log.debug("Initial monitor update failed: %s", exc)

    scheduler = Scheduler(tick=bot.tick, shutdown=shutdown)
    try:
        scheduler.run()
        if agent is not None:
            agent.update_metrics(None, status="STOPPED")
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        if agent is not None:
            agent.update_metrics(None, status="STOPPED")
    except Exception:
        log.exception("Unhandled exception in trading loop.")
        if agent is not None:
            agent.update_metrics(None, status="ERROR")
        raise
    finally:
        portfolio.save(config.STATE_FILE)
        if agent is not None:
            agent.stop()
        remove_pid_file(config.ALGO_NAME)
        log.info("Bot shutdown. Closed trades today: %d | Day P&L: %.2f",
                 sum(1 for t in portfolio.closed_trades if t["exit_date"] == date.today().isoformat()),
                 portfolio.day_pnl())


if __name__ == "__main__":
    main()
