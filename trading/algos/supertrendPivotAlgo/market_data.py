"""
market_data.py
Fetch spot price and historical candle data from SmartAPI.
"""

import time

import pandas as pd

import config
from logger import get_logger

log = get_logger()

IST_TZ = "Asia/Kolkata"


class MarketData:
    def __init__(self, smart):
        self.smart = smart
        self.cfg = config.get_index_config()
        self._stream = None  # optional LiveCandleStream
        # Cached REST candle history to avoid hammering Angel's rate-limited
        # historical endpoint on every 5-minute tick.
        self._rest_cache = None       # pandas.DataFrame or None
        self._rest_cache_time = 0.0   # monotonic timestamp of last successful fetch

    def attach_stream(self, stream):
        """Attach a LiveCandleStream so candles are built from live ticks."""
        self._stream = stream

    def get_spot_price(self):
        """Return the latest traded price (LTP) of the configured index."""
        # Prefer the freshest live tick when a stream is attached.
        if self._stream is not None:
            live = self._stream.get_live_candles(include_forming=True)
            if live:
                return float(live[-1]["close"])
        try:
            resp = self.smart.ltpData(
                self.cfg["spot_exchange"],
                self.cfg["spot_symbol"],
                self.cfg["spot_token"],
            )
            ltp = resp["data"]["ltp"]
            return float(ltp)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to fetch spot price: %s", exc)
            raise

    def get_candles(self, lookback_days=5):
        """
        Fetch historical 5-minute candles for the index spot.

        The REST history is cached and refreshed infrequently when a live tick
        stream is attached (the older bars never change intraday and the stream
        supplies the fresh ones). This keeps us well under Angel One's strict
        historical-data rate limit. On a fetch failure we fall back to the last
        good cache so a transient rate-limit response never aborts the strategy.

        Returns
        -------
        pandas.DataFrame with columns: [datetime, open, high, low, close, volume]
        """
        rest_df = self._get_rest_candles(lookback_days)

        # Overlay live tick-built candles (if a stream is attached) so the most
        # recent, still-forming candle reflects real-time price action.
        return self._merge_live_candles(rest_df)

    def _get_rest_candles(self, lookback_days):
        """Return cached REST candles when fresh, else (re)fetch with fallback."""
        # A long cache TTL is only safe when a live stream supplies intraday
        # candles; without it the strategy must always see the newest REST bar.
        ttl = config.REST_CANDLE_CACHE_SECONDS if self._stream is not None else 0
        age = time.monotonic() - self._rest_cache_time
        if self._rest_cache is not None and age < ttl:
            return self._rest_cache

        try:
            df = self._fetch_rest_candles(lookback_days)
        except Exception:  # noqa: BLE001 - already logged in _fetch_rest_candles
            if self._rest_cache is not None:
                log.warning(
                    "Reusing cached candle history (age %.0fs) after fetch failure.",
                    age,
                )
                return self._rest_cache
            raise

        self._rest_cache = df
        self._rest_cache_time = time.monotonic()
        return df

    def _fetch_rest_candles(self, lookback_days):
        """Fetch and normalize REST candles, retrying on rate-limit errors."""
        # Use IST wall clock so the request window matches the exchange session
        # regardless of the host machine's local timezone.
        to_date = pd.Timestamp.now(tz=IST_TZ).tz_localize(None)
        from_date = to_date - pd.Timedelta(days=lookback_days)

        params = {
            "exchange": self.cfg["spot_exchange"],
            "symboltoken": self.cfg["spot_token"],
            "interval": config.CANDLE_INTERVAL,
            "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
            "todate": to_date.strftime("%Y-%m-%d %H:%M"),
        }

        rows = None
        for attempt in range(1, config.CANDLE_FETCH_MAX_RETRIES + 1):
            try:
                resp = self.smart.getCandleData(params)
                rows = resp["data"]
                break
            except Exception as exc:  # noqa: BLE001
                if attempt < config.CANDLE_FETCH_MAX_RETRIES:
                    delay = config.CANDLE_FETCH_BACKOFF_SECONDS * attempt
                    log.warning(
                        "Candle fetch attempt %d/%d failed (%s); retrying in %.1fs.",
                        attempt, config.CANDLE_FETCH_MAX_RETRIES, exc, delay,
                    )
                    time.sleep(delay)
                else:
                    log.error(
                        "Failed to fetch candle data after %d attempts: %s",
                        config.CANDLE_FETCH_MAX_RETRIES, exc,
                    )
                    raise

        df = pd.DataFrame(
            rows, columns=["datetime", "open", "high", "low", "close", "volume"]
        )
        df["datetime"] = pd.to_datetime(df["datetime"])
        # Angel REST candles are tz-aware IST (+05:30). Convert to naive IST so
        # they align with the live tick candles (which are naive IST too).
        df["datetime"] = self._to_naive_ist(df["datetime"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna().reset_index(drop=True)
        return df

    @staticmethod
    def _to_naive_ist(series):
        """Normalize a datetime series to naive IST (drop tz after converting)."""
        if series.dt.tz is not None:
            series = series.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        return series

    def _merge_live_candles(self, rest_df):
        """
        Merge *completed* live tick candles on top of the REST history.

        Live candles take precedence for any overlapping timestamp. Only
        completed candles are merged (not the still-forming one), so the
        strategy always evaluates a closed 5-minute bar. REST data supplies the
        multi-day history needed for pivots and Supertrend warm-up.
        """
        if self._stream is None:
            return rest_df

        # include_forming=False: never feed a partial candle to the strategy.
        live = self._stream.get_live_candles(include_forming=False)
        if not live:
            return rest_df

        live_df = pd.DataFrame(live)
        live_df["datetime"] = pd.to_datetime(live_df["datetime"])

        # Drop pre-open bars (e.g. 09:00/09:05/09:10). The live tick stream
        # aggregates pre-open auction ticks into candles that Angel One's chart
        # does NOT include; feeding them into Supertrend's Wilder ATR pollutes
        # the bands for the first ~15 bars of the session (the pre-open bar can
        # span 200+ points vs the usual ~40-point 5-min range) and causes
        # spurious direction flips later in the day.
        market_open = pd.Timestamp(config.MARKET_OPEN).time()
        live_df = live_df[live_df["datetime"].dt.time >= market_open]

        if live_df.empty:
            return rest_df

        # Defense in depth: if more than one live candle ever shares a timestamp
        # (e.g. a real bar followed by a degenerate single-tick replay built
        # from a late tick), keep the FIRST — the real bar built from the full
        # set of ticks within the bucket. CandleAggregator now rejects such
        # late ticks, so this dedupe is a safety net.
        live_df = live_df.drop_duplicates(subset="datetime", keep="first")

        combined = pd.concat([rest_df, live_df], ignore_index=True)
        # Live rows come last, so keep="last" lets them override REST bars.
        combined = combined.drop_duplicates(
            subset="datetime", keep="last"
        ).sort_values("datetime").reset_index(drop=True)
        return combined

