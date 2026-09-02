"""
Phase 7 -- in-memory live cache.

Live quotes are kept ONLY here (rule 17); PostgreSQL gets 1-minute candles
(see aggregator.py), never ticks. Thread-safe: ticks arrive on the
provider's websocket thread while the API reads from the event loop.

Stale detection: a symbol whose last update is older than
``MARKET_DATA_STALE_SECONDS`` reports ``status == "stale"``.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from trading.market_data.schemas import IndexQuote, OptionQuote


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CachedQuote:
    quote: IndexQuote | OptionQuote
    provider_timestamp: datetime | None
    received_at: datetime

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or _now()) - self.received_at).total_seconds()

    def is_stale(self, stale_seconds: float, now: datetime | None = None) -> bool:
        return self.age_seconds(now) > stale_seconds

    def status(self, stale_seconds: float, now: datetime | None = None) -> str:
        return "stale" if self.is_stale(stale_seconds, now) else "live"


class LiveCache:
    def __init__(self, *, stale_seconds: float = 10.0) -> None:
        self.stale_seconds = float(stale_seconds)
        self._lock = threading.RLock()
        self._indices: dict[str, CachedQuote] = {}     # internal symbol -> CachedQuote
        self._options: dict[str, CachedQuote] = {}     # internal option symbol -> CachedQuote
        self._last_tick_at: datetime | None = None

    # --- writes ---------------------------------------------------
    def put(self, quote: IndexQuote | OptionQuote) -> None:
        entry = CachedQuote(
            quote=quote,
            provider_timestamp=quote.provider_timestamp,
            received_at=quote.received_at or _now(),
        )
        with self._lock:
            if isinstance(quote, IndexQuote):
                self._indices[quote.symbol] = entry
            else:
                self._options[quote.symbol] = entry
            self._last_tick_at = entry.received_at

    # --- reads ----------------------------------------------------
    def get_latest_quote(self, symbol: str) -> CachedQuote | None:
        with self._lock:
            return self._indices.get(symbol) or self._options.get(symbol)

    def get_option_quote(self, symbol: str) -> CachedQuote | None:
        with self._lock:
            return self._options.get(symbol)

    def all_indices(self) -> dict[str, CachedQuote]:
        with self._lock:
            return dict(self._indices)

    def all_options(self) -> dict[str, CachedQuote]:
        with self._lock:
            return dict(self._options)

    def is_stale(self, symbol: str, now: datetime | None = None) -> bool:
        entry = self.get_latest_quote(symbol)
        return entry is None or entry.is_stale(self.stale_seconds, now)

    @property
    def last_tick_at(self) -> datetime | None:
        with self._lock:
            return self._last_tick_at

    def symbols_live(self, now: datetime | None = None) -> int:
        with self._lock:
            entries = list(self._indices.values()) + list(self._options.values())
        return sum(1 for e in entries if not e.is_stale(self.stale_seconds, now))

    def clear(self) -> None:
        with self._lock:
            self._indices.clear()
            self._options.clear()
            self._last_tick_at = None
