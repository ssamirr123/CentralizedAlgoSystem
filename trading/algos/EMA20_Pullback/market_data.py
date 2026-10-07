"""
Live market data for EMA20_Pullback.

Broker-agnostic: dispatches candle + LTP fetches to AngelOne SmartAPI or
Dhan dhanhq based on `BrokerAdapter.kind`. Output shape stays constant
so `strategy.py` is unchanged across brokers.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Optional

import pandas as pd

from trading.algos.EMA20_Pullback import config
from trading.algos.EMA20_Pullback.logger import get_logger
from trading.algos.EMA20_Pullback.login import BrokerAdapter
from trading.algos.EMA20_Pullback.option_chain import OptionChain

log = get_logger()

IST_TZ = "Asia/Kolkata"


class MarketData:
    def __init__(self, broker: BrokerAdapter, option_chain: OptionChain) -> None:
        self.broker = broker
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

        if self.broker.kind == "ANGELONE":
            df = self._angel_candles(from_date, to_date)
        elif self.broker.kind == "DHAN":
            df = self._dhan_candles(from_date, to_date)
        else:
            raise RuntimeError(f"Unsupported broker kind: {self.broker.kind!r}")

        self._rest_cache = df
        self._rest_cache_time = time.monotonic()
        return df

    def _angel_candles(self, from_date: pd.Timestamp, to_date: pd.Timestamp) -> pd.DataFrame:
        params = {
            "exchange": self.cfg["spot_exchange"],
            "symboltoken": self.cfg["spot_token"],
            "interval": config.CANDLE_INTERVAL,
            "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
            "todate": to_date.strftime("%Y-%m-%d %H:%M"),
        }
        rows = self._with_retries(lambda: self.broker.client.getCandleData(params)["data"], "candles")
        df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])
        return self._normalize_candles(df)

    def _dhan_candles(self, from_date: pd.Timestamp, to_date: pd.Timestamp) -> pd.DataFrame:
        payload_dict = self._with_retries(
            lambda: self.broker.client.intraday_minute_data(
                security_id=str(self.cfg["dhan_spot_security_id"]),
                exchange_segment=self.cfg["dhan_spot_exchange_segment"],
                instrument_type=self.cfg["dhan_spot_instrument_type"],
                from_date=from_date.strftime("%Y-%m-%d"),
                to_date=to_date.strftime("%Y-%m-%d"),
            ),
            "candles",
        )
        data = (payload_dict or {}).get("data") or payload_dict
        if not data or "open" not in data:
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame({
            "datetime": pd.to_datetime(data.get("timestamp", data.get("start_Time", [])), unit="s"),
            "open":   data.get("open", []),
            "high":   data.get("high", []),
            "low":    data.get("low", []),
            "close":  data.get("close", []),
            "volume": data.get("volume", [0] * len(data.get("open", []))),
        })
        return self._normalize_candles(df)

    @staticmethod
    def _normalize_candles(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        df["datetime"] = pd.to_datetime(df["datetime"])
        if df["datetime"].dt.tz is not None:
            df["datetime"] = df["datetime"].dt.tz_convert(IST_TZ).dt.tz_localize(None)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna(subset=["open", "close"]).reset_index(drop=True)

    def _with_retries(self, call, label: str):
        for attempt in range(1, config.CANDLE_FETCH_MAX_RETRIES + 1):
            try:
                return call()
            except Exception as exc:  # noqa: BLE001
                if attempt < config.CANDLE_FETCH_MAX_RETRIES:
                    delay = config.CANDLE_FETCH_BACKOFF_SECONDS * attempt
                    log.warning("%s fetch %d/%d failed (%s); retry in %.1fs",
                                label, attempt, config.CANDLE_FETCH_MAX_RETRIES, exc, delay)
                    time.sleep(delay)
                else:
                    log.error("%s fetch exhausted retries: %s", label, exc)
                    if self._rest_cache is not None:
                        return self._rest_cache
                    raise

    def option_ltp(self, strike: int, option_type: str,
                   expiry: Optional[date] = None) -> Optional[float]:
        try:
            contract = self._option_chain.resolve(strike, option_type, expiry)
        except LookupError as exc:
            log.warning("Option symbol resolution failed: %s", exc)
            return None
        if self.broker.kind == "ANGELONE":
            return self._angel_ltp(contract["symbol"], str(contract["token"]))
        if self.broker.kind == "DHAN":
            return self._dhan_ltp(contract.get("exchange_segment", "NSE_FNO"),
                                   int(contract["token"]))
        return None

    def _angel_ltp(self, tradingsymbol: str, token: str) -> Optional[float]:
        for attempt in range(1, 4):
            try:
                resp = self.broker.client.ltpData(self.cfg["exchange"], tradingsymbol, token)
                return float(resp["data"]["ltp"])
            except Exception as exc:  # noqa: BLE001
                if attempt < 3:
                    time.sleep(0.4 * attempt)
                    continue
                log.warning("AngelOne LTP failed for %s (%s)", tradingsymbol, exc)
                return None
        return None

    def _dhan_ltp(self, exchange_segment: str, security_id: int) -> Optional[float]:
        for attempt in range(1, 4):
            try:
                resp = self.broker.client.ohlc_data({exchange_segment: [security_id]})
                data = (resp or {}).get("data") or {}
                row = data.get(exchange_segment, {}).get(str(security_id)) \
                      or data.get(exchange_segment, {}).get(security_id)
                if row is None:
                    return None
                ltp = row.get("last_price") or row.get("ltp") or row.get("close")
                return float(ltp) if ltp is not None else None
            except Exception as exc:  # noqa: BLE001
                if attempt < 3:
                    time.sleep(0.4 * attempt)
                    continue
                log.warning("Dhan LTP failed for sec_id=%s (%s)", security_id, exc)
                return None
        return None

    def next_weekly_expiry(self) -> date:
        return self._option_chain.next_weekly_expiry()
