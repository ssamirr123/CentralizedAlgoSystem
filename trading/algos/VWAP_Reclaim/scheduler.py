"""
Tick-loop driver for VWAP_Reclaim.

Thin wrapper that runs a fixed-interval tick during the market window
and respects a `GracefulShutdown` set by the dashboard Stop button or by
SIGTERM/Ctrl+C. Mirrors `supertrendPivotAlgo.scheduler.BotScheduler`'s
responsibilities but on a seconds cadence (intraday option work needs
tighter polling than 5-min boundaries).
"""
from __future__ import annotations

import time
from datetime import datetime, time as dtime
from typing import Callable

from trading.algos.VWAP_Reclaim import config
from trading.algos.VWAP_Reclaim.logger import get_logger

log = get_logger()


def _parse_hhmm(value: str) -> dtime:
    h, m = value.split(":")
    return dtime(int(h), int(m))


class Scheduler:
    def __init__(self, tick: Callable[[datetime], None], shutdown=None,
                 interval_seconds: float | None = None) -> None:
        self._tick = tick
        self._shutdown = shutdown
        self._interval = float(interval_seconds if interval_seconds is not None else config.LOOP_INTERVAL_SECONDS)
        self._market_open = _parse_hhmm(config.MARKET_OPEN)
        self._market_close = _parse_hhmm(config.MARKET_CLOSE)

    def _in_window(self, now: datetime) -> bool:
        return now.weekday() < 5 and self._market_open <= now.time() <= self._market_close

    def run(self) -> None:
        log.info("Scheduler started | window %s-%s | tick=%ss",
                 config.MARKET_OPEN, config.MARKET_CLOSE, self._interval)
        while True:
            if self._shutdown is not None and self._shutdown.is_set():
                log.warning("Stop requested; leaving scheduler loop.")
                return
            now = datetime.now()
            try:
                # Still tick outside market hours so the monitor keeps heartbeating.
                self._tick(now)
            except Exception as exc:  # noqa: BLE001
                log.exception("Error during tick: %s", exc)
            if self._shutdown is not None:
                self._shutdown.wait(self._interval)
            else:
                time.sleep(self._interval)
