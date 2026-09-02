"""
Phase 8 -- 1-minute aggregation.

    tick -> live cache -> aggregator -> OHLC candle -> PostgreSQL

Exactly ONE row per (symbol/contract, minute) reaches the database
(rule 16). Ticks update an in-memory minute bucket; ``flush(now)`` closes
every bucket whose minute has ended and returns the finished candles for
a single batched write.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trading.database import models
from trading.market_data.schemas import Candle

logger = logging.getLogger("trading.market_data.aggregator")


def _minute_start(ts: datetime) -> datetime:
    ts = ts.astimezone(timezone.utc)
    return ts.replace(second=0, microsecond=0)


@dataclass
class _Bucket:
    minute: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    oi: int | None = None

    def update(self, price: float, volume: int | None, oi: int | None) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        if volume is not None:
            self.volume = volume  # provider sends cumulative day volume; last wins
        if oi is not None:
            self.oi = oi


@dataclass
class _Series:
    key: tuple  # ("index", symbol, exchange) | ("option", contract_id)
    current: _Bucket | None = None
    finished: list[_Bucket] = field(default_factory=list)
    last_closed: datetime | None = None  # newest minute already emitted

    def add(self, ts: datetime, price: float, volume: int | None, oi: int | None) -> None:
        m = _minute_start(ts)
        if self.last_closed is not None and m <= self.last_closed:
            return  # late tick for an already-closed minute -> drop
        if self.current is None:
            self.current = _Bucket(m, price, price, price, price, volume, oi)
        elif m == self.current.minute:
            self.current.update(price, volume, oi)
        elif m > self.current.minute:
            self.finished.append(self.current)
            self.current = _Bucket(m, price, price, price, price, volume, oi)
        # m < current.minute (but > last_closed) -> a gap-fill for a minute
        # we never opened; drop rather than reorder.

    def close_through(self, now_minute: datetime) -> list[_Bucket]:
        out = list(self.finished)
        self.finished.clear()
        if self.current is not None and self.current.minute < now_minute:
            out.append(self.current)
            self.current = None
        for b in out:
            if self.last_closed is None or b.minute > self.last_closed:
                self.last_closed = b.minute
        return out


class CandleAggregator:
    def __init__(self, *, interval: str = "1minute") -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._index: dict[tuple, _Series] = {}
        self._option: dict[int, _Series] = {}

    # --- ingest ------------------------------------------------
    def on_index_tick(
        self, symbol: str, exchange: str, price: float, ts: datetime,
        *, volume: int | None = None, oi: int | None = None,
    ) -> None:
        key = ("index", symbol, exchange)
        with self._lock:
            self._index.setdefault(key, _Series(key)).add(ts, price, volume, oi)

    def on_option_tick(
        self, contract_id: int, price: float, ts: datetime,
        *, volume: int | None = None, oi: int | None = None,
    ) -> None:
        with self._lock:
            self._option.setdefault(contract_id, _Series(("option", contract_id))).add(ts, price, volume, oi)

    # --- close ------------------------------------------------
    def flush(self, now: datetime) -> tuple[list[tuple[str, str, Candle]], list[tuple[int, Candle]]]:
        """Return (index_candles, option_candles) for every fully-elapsed
        minute. index item: (symbol, exchange, Candle); option item:
        (contract_id, Candle). Buckets for the still-current minute stay."""
        now_minute = _minute_start(now)
        idx_out: list[tuple[str, str, Candle]] = []
        opt_out: list[tuple[int, Candle]] = []
        with self._lock:
            for (_, symbol, exchange), series in self._index.items():
                for b in series.close_through(now_minute):
                    idx_out.append((symbol, exchange, _to_candle(symbol, self.interval, b)))
            for contract_id, series in self._option.items():
                for b in series.close_through(now_minute):
                    opt_out.append((contract_id, _to_candle(f"contract:{contract_id}", self.interval, b)))
        return idx_out, opt_out

    def force_close_all(self, now: datetime) -> tuple[list, list]:
        """End-of-day: close the current minute too."""
        with self._lock:
            for series in list(self._index.values()) + list(self._option.values()):
                if series.current is not None:
                    series.finished.append(series.current)
                    series.current = None
        return self.flush(now + timedelta(minutes=1))


def _to_candle(symbol: str, interval: str, b: _Bucket) -> Candle:
    return Candle(
        symbol=symbol, interval=interval, timestamp=b.minute,
        open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume, oi=b.oi,
    )


# --- persistence (batched, idempotent) ----------------------------------
# Idempotency: a fast pre-check skips rows that already exist, then the
# commit itself is wrapped so a race / SQLite-vs-Postgres timestamp
# round-trip quirk falls back to per-row inserts, never a lost minute.
def persist_index_candles(db: Session, items: list[tuple[str, str, Candle]]) -> int:
    if not items:
        return 0
    existing = _existing_index_keys(db, items)
    new_rows = [
        (symbol, exchange, c)
        for symbol, exchange, c in items
        if (symbol, exchange, c.interval, _norm_ts(c.timestamp)) not in existing
    ]
    return _commit_rows(
        db,
        [
            models.MarketCandle(
                timestamp=c.timestamp, symbol=symbol, exchange=exchange, interval=c.interval,
                open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume, oi=c.oi,
            )
            for symbol, exchange, c in new_rows
        ],
    )


def persist_option_candles(db: Session, items: list[tuple[int, Candle]]) -> int:
    if not items:
        return 0
    existing = _existing_option_keys(db, items)
    new_rows = [(cid, c) for cid, c in items if (cid, _norm_ts(c.timestamp)) not in existing]
    return _commit_rows(
        db,
        [
            models.OptionCandle(
                timestamp=c.timestamp, contract_id=cid,
                open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume, oi=c.oi,
            )
            for cid, c in new_rows
        ],
    )


def _commit_rows(db: Session, rows: list) -> int:
    if not rows:
        return 0
    try:
        db.add_all(rows)
        db.commit()
        return len(rows)
    except IntegrityError:
        db.rollback()
        written = 0
        for row in rows:
            try:
                db.add(row)
                db.commit()
                written += 1
            except IntegrityError:
                db.rollback()  # already present -> fine
        return written


def _existing_index_keys(db: Session, items) -> set[tuple]:
    symbols = {s for s, _, _ in items}
    lo = min(c.timestamp for _, _, c in items)
    hi = max(c.timestamp for _, _, c in items)
    rows = db.execute(
        select(
            models.MarketCandle.symbol, models.MarketCandle.exchange,
            models.MarketCandle.interval, models.MarketCandle.timestamp,
        ).where(
            models.MarketCandle.symbol.in_(symbols),
            models.MarketCandle.timestamp >= lo,
            models.MarketCandle.timestamp <= hi,
        )
    ).all()
    return {(r[0], r[1], r[2], _norm_ts(r[3])) for r in rows}


def _existing_option_keys(db: Session, items) -> set[tuple]:
    cids = {cid for cid, _ in items}
    lo = min(c.timestamp for _, c in items)
    hi = max(c.timestamp for _, c in items)
    rows = db.execute(
        select(models.OptionCandle.contract_id, models.OptionCandle.timestamp).where(
            models.OptionCandle.contract_id.in_(cids),
            models.OptionCandle.timestamp >= lo,
            models.OptionCandle.timestamp <= hi,
        )
    ).all()
    return {(r[0], _norm_ts(r[1])) for r in rows}


def _norm_ts(ts: datetime) -> datetime:
    """SQLite returns DateTime(timezone=True) columns as naive; the app's
    Candle.timestamp is tz-aware UTC. Compare on naive-UTC so the
    pre-check matches on both backends."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    return ts.replace(tzinfo=None)
