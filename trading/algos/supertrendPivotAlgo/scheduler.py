"""
scheduler.py
Drives the bot on every completed 5-minute candle between market open and the
square-off time, then forces an end-of-day square-off.
"""

import time
from datetime import datetime, time as dtime

import schedule

import config
from logger import get_logger

log = get_logger()


def _parse_hhmm(value):
    h, m = value.split(":")
    return dtime(int(h), int(m))


class BotScheduler:
    def __init__(self, strategy, order_manager, shutdown=None):
        self.strategy = strategy
        # Optional trading.common GracefulShutdown: dashboard Stop / SIGTERM.
        self.shutdown = shutdown
        self.om = order_manager
        self.market_open = _parse_hhmm(config.MARKET_OPEN)
        self.first_candle = _parse_hhmm(config.FIRST_CANDLE_CLOSE)
        self.square_off = _parse_hhmm(config.SQUARE_OFF_TIME)
        self._squared_off = False

    def _within_session(self, now):
        return self.first_candle <= now <= self.square_off

    def tick(self):
        """Called every 5 minutes; runs evaluation or square-off."""
        now = datetime.now().time()

        if now >= self.square_off:
            self._do_square_off()
            return

        if not self._within_session(now):
            log.debug("Outside trading session (%s). Skipping.", now)
            return

        try:
            self.strategy.evaluate()
        except Exception as exc:  # noqa: BLE001
            log.exception("Error during strategy evaluation: %s", exc)

    def _do_square_off(self):
        """Square off once at end of day and mark the session finished.

        The finished flag is set *even if* square-off raises, so a failing
        broker call can never keep the scheduler looping past the square-off
        time (which previously only stopped on a KeyboardInterrupt).
        """
        if self._squared_off:
            return
        log.info("Square-off time reached (%s).", config.SQUARE_OFF_TIME)
        try:
            self.om.square_off_all()
        except Exception as exc:  # noqa: BLE001
            log.exception("Square-off failed: %s", exc)
        finally:
            self._squared_off = True

    def run(self):
        """Block and run the schedule loop until after square-off."""
        log.info(
            "Scheduler started. Session %s-%s, max %d trades/day.",
            config.FIRST_CANDLE_CLOSE, config.SQUARE_OFF_TIME,
            config.MAX_TRADES_PER_DAY,
        )

        # Run on every 5-minute boundary (:00, :05, :10, ...).
        for minute in range(0, 60, 5):
            schedule.every().hour.at(f":{minute:02d}").do(self.tick)

        # Run once immediately so we don't wait for the next boundary.
        self.tick()

        while not self._squared_off:
            if self.shutdown is not None and self.shutdown.is_set():
                # Dashboard Stop: never leave a short open behind.
                log.warning("Stop requested - squaring off before exit.")
                self._do_square_off()
                break
            schedule.run_pending()
            # Wall-clock safety net: guarantee the session ends at the
            # square-off time even if a scheduled tick is missed or delayed.
            if datetime.now().time() >= self.square_off:
                self._do_square_off()
                break
            if self.shutdown is not None:
                self.shutdown.wait(5)       # wakes immediately on Stop
            else:
                time.sleep(5)

        # Drop any pending jobs so a lingering schedule can't refire.
        schedule.clear()

        log.info("Trading session complete. Final day P&L: %.2f",
                 self.om.realized_pnl)
