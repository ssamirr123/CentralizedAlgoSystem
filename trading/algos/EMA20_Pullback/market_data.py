"""
Live market data for EMA20_Pullback via Angel One SmartAPI.

Spot: 1-minute NIFTY candles from Angel's historical-data endpoint
(cached + retried to survive SmartAPI's rate limits).
Option LTP: Angel's `ltpData` on NFO via `OptionChain`-resolved symbol+token.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from trading.algos.EMA20_Pullback import config
from trading.algos.EMA20_Pullback.logger import get_logger
from trading.algos.EMA20_Pullback.option_chain import OptionChain

log = get_logger()

IST_TZ = "Asia/Kolkata"


class MarketData:
    def __init__(self, smart, option_chain: OptionChain) -> None:
        self.smart = smart
        self.cfg = config.get_index_config()
        self._option_chain = option_chain
        self._rest_cache: Optional[pd.DataFrame] = None
        self._rest_cache_time = 0.0

    def fetch_spot_minutes(self, today: date) -> pd.DataFrame:
        try:
            df = self._fetch_rest_candles(lookback_days=1)
        except Exception as exc:  # noqa: BLE001
            log.error("Spot candle fetch failed: %s", exc)
            return pd.DataFrame()
        if df.empty:
            return df
        df = df[df["datetime"].dt.date == today]
        if df.empty:
            return df
        out = df.set_index("datetime")[["open", "high", "low", "close"]]
        out.columns = ["Open", "High", "Low", "Close"]
        return out

    def _fetch_rest_candles(self, lookback_days: int = 1) -> pd.DataFrame:
        ttl = config.REST_CANDLE_CACHE_SECONDS
        age = time.monotonic() - self._rest_cache_time
        if self._rest_cache is not None and age < ttl:
            return self._rest_cache

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
                    log.warning("Candle fetch %d/%d failed (%s); retry in %.1fs",
                                attempt, config.CANDLE_FETCH_MAX_RETRIES, exc, delay)
                    time.sleep(delay)
                else:
                    log.error("Candle fetch exhausted retries: %s", exc)
                    if self._rest_cache is not None:
                        return self._rest_cache
                    raise

        df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        if df["datetime"].dt.tz is not None:
            df["datetime"] = df["datetime"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna().reset_index(drop=True)

        self._rest_cache = df
        self._rest_cache_time = time.monotonic()
        return df

    def option_ltp(self, strike: int, option_type: str,
                   expiry: Optional[date] = None) -> Optional[float]:
        try:
            contract = self._option_chain.resolve(strike, option_type, expiry)
        except LookupError as exc:
            log.warning("Option symbol resolution failed: %s", exc)
            return None
        return self._ltp(contract["symbol"], contract["token"])

    def _ltp(self, tradingsymbol: str, token: str) -> Optional[float]:
        for attempt in range(1, 4):
            try:
                resp = self.smart.ltpData(self.cfg["exchange"], tradingsymbol, token)
                return float(resp["data"]["ltp"])
            except Exception as exc:  # noqa: BLE001
                if attempt < 3:
                    time.sleep(0.4 * attempt)
                    continue
                log.warning("LTP fetch failed for %s (%s)", tradingsymbol, exc)
                return None
        return None

    def next_weekly_expiry(self) -> date:
        return self._option_chain.next_weekly_expiry()
