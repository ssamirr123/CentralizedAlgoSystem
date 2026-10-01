"""
tick_stream.py
Stream live ticks from Angel One's SmartWebSocketV2 and aggregate them into
fixed-interval (default 5-minute) OHLCV candles.

Two building blocks:
    CandleAggregator  -> pure, thread-safe tick -> candle bucketing logic.
    LiveCandleStream  -> runs SmartWebSocketV2 in a background thread and feeds
                         every tick into a CandleAggregator.

The stream is designed to sit *next to* the existing REST candle history:
MarketData merges the completed live candles on top of the REST base so the
strategy transparently trades on the freshest data.
"""

import threading
from datetime import datetime, timedelta, timezone
import time

import config
from logger import get_logger

log = get_logger()

# Indian market timezone. Angel One REST candles are stamped in IST (+05:30),
# so live candles must use the same wall clock for the merge to align.
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    """Current time as a naive IST datetime (matches REST candle stamps)."""
    return datetime.now(IST).replace(tzinfo=None)


def epoch_ms_to_ist(ms):
    """Convert an epoch-millisecond timestamp to a naive IST datetime."""
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).astimezone(
        IST
    ).replace(tzinfo=None)

# Import path differs between smartapi-python releases.
try:  # newer
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
except ImportError:  # pragma: no cover - older / alternative packaging
    try:
        from smartapi.smartWebSocketV2 import SmartWebSocketV2
    except ImportError:
        SmartWebSocketV2 = None


def _floor_to_bucket(ts, minutes):
    """Floor a datetime to the start of its N-minute candle bucket."""
    discard = timedelta(
        minutes=ts.minute % minutes,
        seconds=ts.second,
        microseconds=ts.microsecond,
    )
    return ts - discard


class CandleAggregator:
    """
    Aggregate a stream of (price, volume, timestamp) ticks into OHLCV candles.

    Thread-safe: ticks arrive on the WebSocket thread while the strategy thread
    reads snapshots.
    """

    def __init__(self, interval_minutes=None):
        self.interval = interval_minutes or config.CANDLE_INTERVAL_MINUTES
        self._lock = threading.Lock()
        self._completed = []          # list of finished candle dicts
        self._current = None          # the forming candle dict
        self._last_volume = None      # cumulative volume from feed (if any)
        # Timestamp of the most recently finalized bucket. Used to reject late
        # ticks that would otherwise resurrect a closed bar (WebSocket latency
        # can deliver ticks whose exchange_timestamp still falls inside the
        # just-closed bucket, which previously produced a second degenerate
        # H=L=O=C bar at the same timestamp).
        self._last_finalized_bucket = None

    def add_tick(self, price, timestamp=None, cumulative_volume=None):
        """
        Feed one tick into the aggregator.

        Parameters
        ----------
        price : float
            Last traded price of the tick.
        timestamp : datetime, optional
            Tick time. Defaults to now() when the feed omits it.
        cumulative_volume : int, optional
            Day-cumulative traded volume from the feed. Per-candle volume is
            derived as the delta across the candle.
        """
        if price is None:
            return

        ts = timestamp or now_ist()
        bucket = _floor_to_bucket(ts, self.interval)

        with self._lock:
            # Reject late ticks for an already-finalized bucket. Without this,
            # a tick arriving after finalize_due() cleared _current would spawn
            # a fresh single-tick bar at the old bucket, producing a duplicate
            # degenerate H=L=O=C candle at the same timestamp.
            if (
                self._last_finalized_bucket is not None
                and bucket <= self._last_finalized_bucket
            ):
                return

            if self._current is None:
                self._current = self._new_candle(bucket, price)
            elif bucket > self._current["datetime"]:
                # Bucket rolled over -> finalize the previous candle.
                self._finalize_locked()
                self._current = self._new_candle(bucket, price)

            c = self._current
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price

            if cumulative_volume is not None:
                if self._last_volume is not None and cumulative_volume >= self._last_volume:
                    c["volume"] += cumulative_volume - self._last_volume
                self._last_volume = cumulative_volume

    def finalize_due(self, now=None):
        """
        Close the current candle if the wall clock has passed its bucket end.

        This mirrors the reference project's boundary-flush behaviour: candles
        close exactly on the interval boundary (e.g. :00, :05) even when ticks
        are sparse, so the scheduler always reads a *completed* last candle.
        Returns True if a candle was finalized.
        """
        now = now or now_ist()
        with self._lock:
            if self._current is None:
                return False
            bucket_end = self._current["datetime"] + timedelta(minutes=self.interval)
            if now >= bucket_end:
                self._finalize_locked()
                self._current = None
                return True
        return False

    def _finalize_locked(self):
        """Move the forming candle into the completed list. Caller holds lock."""
        c = self._current
        if c is None:
            return
        self._completed.append(c)
        self._last_finalized_bucket = c["datetime"]
        log.info(
            "Live candle closed %s O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f",
            c["datetime"], c["open"], c["high"], c["low"], c["close"], c["volume"],
        )

    def _new_candle(self, bucket, price):
        return {
            "datetime": bucket,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 0,
        }

    def snapshot(self, include_forming=False):
        """
        Return a list of candle dicts collected so far.

        Parameters
        ----------
        include_forming : bool
            When True, append the still-forming (incomplete) current candle.
        """
        with self._lock:
            out = list(self._completed)
            if include_forming and self._current is not None:
                out.append(dict(self._current))
        return out


class LiveCandleStream:
    """
    Manage a SmartWebSocketV2 connection that streams the index spot ticks and
    aggregates them into candles via a CandleAggregator.
    """

    def __init__(self, smart, interval_minutes=None):
        self.smart = smart
        self.cfg = config.get_index_config()
        self.aggregator = CandleAggregator(interval_minutes)
        self.ws = None
        self._thread = None
        self._flush_thread = None
        self._running = False
        self._correlation_id = "st_pivot_bot"
        # LTP mode = 1 (lightweight). Use 3 (SNAP QUOTE) to also get volume.
        self._mode = 1

    # ---------------------------------------------------------- public helpers
    def start(self):
        """Open the WebSocket in a background daemon thread."""
        if SmartWebSocketV2 is None:
            log.error(
                "SmartWebSocketV2 not available in installed smartapi package; "
                "live stream disabled."
            )
            return False

        auth_token = getattr(self.smart, "jwt_token", None)
        feed_token = getattr(self.smart, "feed_token", None) or self._safe_feed_token()

        if not auth_token or not feed_token:
            log.error("Missing auth/feed token; cannot start live stream.")
            return False

        self.ws = SmartWebSocketV2(
            auth_token,
            config.apikey,
            config.clientid,
            feed_token,
        )
        self.ws.on_open = self._on_open
        self.ws.on_data = self._on_data
        self.ws.on_error = self._on_error
        self.ws.on_close = self._on_close

        self._thread = threading.Thread(
            target=self.ws.connect, name="LiveCandleStream", daemon=True
        )
        self._thread.start()

        # Boundary-flush thread: closes candles exactly on the interval edge
        # (mirrors the reference project's savedata_ohlc timing).
        self._running = True
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="CandleFlush", daemon=True
        )
        self._flush_thread.start()

        log.info("Live tick stream starting for %s...", self.cfg["spot_symbol"])
        return True

    def stop(self):
        """Close the WebSocket connection if open."""
        self._running = False
        try:
            if self.ws is not None:
                self.ws.close_connection()
        except Exception as exc:  # noqa: BLE001
            log.warning("Error closing live stream: %s", exc)

    def _flush_loop(self):
        """Poll near each interval boundary and finalize the due candle."""
        while self._running:
            try:
                self.aggregator.finalize_due()
            except Exception as exc:  # noqa: BLE001
                log.error("Candle flush error: %s", exc)
            # Check frequently so candles close promptly on the boundary.
            time.sleep(0.25)

    def get_live_candles(self, include_forming=True):
        """Return candles built from live ticks so far."""
        return self.aggregator.snapshot(include_forming=include_forming)

    # ----------------------------------------------------------- ws callbacks
    def _on_open(self, wsapp):
        token_list = [
            {
                "exchangeType": self.cfg["feed_exchange_type"],
                "tokens": [self.cfg["spot_token"]],
            }
        ]
        self.ws.subscribe(self._correlation_id, self._mode, token_list)
        log.info("Subscribed to live feed: token=%s", self.cfg["spot_token"])

    def _on_data(self, wsapp, message):
        try:
            tick = message if isinstance(message, dict) else {}
            ltp_raw = tick.get("last_traded_price")
            if ltp_raw is None:
                return
            # Angel One sends prices in paise (x100).
            price = float(ltp_raw) / 100.0

            ts_ms = tick.get("exchange_timestamp") or tick.get("last_traded_timestamp")
            if ts_ms:
                ts = epoch_ms_to_ist(ts_ms)
            else:
                ts = now_ist()

            # Day-cumulative traded volume -> expressed in lots (as in the
            # reference: message['volume_trade_for_the_day'] / lot_size).
            vol_raw = tick.get("volume_trade_for_the_day")
            lot_size = self.cfg.get("lot_size") or 1
            volume = (float(vol_raw) / lot_size) if vol_raw is not None else None

            self.aggregator.add_tick(price, timestamp=ts, cumulative_volume=volume)
        except Exception as exc:  # noqa: BLE001
            log.error("Error processing tick: %s", exc)

    def _on_error(self, wsapp, error):
        log.error("Live stream error: %s", error)

    def _on_close(self, wsapp):
        log.warning("Live stream connection closed.")

    # ----------------------------------------------------------------- utils
    def _safe_feed_token(self):
        try:
            return self.smart.getfeedToken()
        except Exception:  # noqa: BLE001
            return None

