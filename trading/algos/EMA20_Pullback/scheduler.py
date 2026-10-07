"""
Tick-loop driver for EMA20_Pullback.

Thin wrapper around a fixed-interval tick during the market window,
respecting `GracefulShutdown`. Mirrors `supertrendPivotAlgo.scheduler`.
"""
from __future__ import annotations

import time
from datetime import datetime, time as dtime
from typing import Callable

from trading.algos.EMA20_Pullback import config
from trading.algos.EMA20_Pullback.logger import get_logger

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
                self._tick(now)
            except Exception as exc:  # noqa: BLE001
                log.exception("Error during tick: %s", exc)
            if self._shutdown is not None:
                self._shutdown.wait(self._interval)
            else:
                time.sleep(self._interval)
