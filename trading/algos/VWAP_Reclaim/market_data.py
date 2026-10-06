"""
Live market data for VWAP_Reclaim.

Spot: yfinance NIFTY 50 (^NSEI) 1-min bars for the current session.
Option LTP: broker adapter via trading.common.broker.get_broker(); when
no broker is wired, returns None and the runner cleanly skips the tick
rather than fabricating a fill.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

_logger = logging.getLogger("trading.VWAP_Reclaim.market_data")

NIFTY_SYMBOL = "^NSEI"


def fetch_spot_minutes(today: date) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError:
        _logger.warning("yfinance not installed; spot minute data unavailable")
        return pd.DataFrame()
    try:
        df = yf.download(NIFTY_SYMBOL, period="2d", interval="1m",
                          auto_adjust=False, progress=False, threads=False)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("yfinance 1m download failed: %s", exc)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Kolkata").tz_localize(None)
    df.index = idx
    df = df[df.index.date == today]
    cols = [c for c in ("Open", "High", "Low", "Close") if c in df.columns]
    return df[cols].dropna()


def next_weekly_expiry(today: date, expiries: list[date]) -> Optional[date]:
    for e in expiries:
        if e >= today:
            return e
    return None


def load_expiry_calendar(csv_path: Optional[Path] = None) -> list[date]:
    if csv_path and csv_path.exists():
        df = pd.read_csv(csv_path)
        col = df.columns[0]
        return sorted(pd.to_datetime(df[col]).dt.date.tolist())
    return []


def thursday_of_week(d: date) -> date:
    return d + timedelta(days=(3 - d.weekday()) % 7)


class OptionQuoteService:
    def __init__(self) -> None:
        self._broker = None
        self._tried = False

    def _lazy_broker(self):
        if self._tried:
            return self._broker
        self._tried = True
        try:
            from trading.common import broker as broker_mod   # noqa: WPS433
            self._broker = broker_mod.get_broker()
        except Exception as exc:  # noqa: BLE001
            _logger.info("no broker adapter available: %s", exc)
            self._broker = None
        return self._broker

    def ltp(self, expiry: date, strike: int, direction: str) -> Optional[float]:
        broker = self._lazy_broker()
        if broker is None:
            return None
        tradingsymbol = self.nifty_option_symbol(expiry, strike, direction)
        try:
            quote = broker.ltp(exchange="NFO", tradingsymbol=tradingsymbol)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("option LTP fetch failed for %s: %s", tradingsymbol, exc)
            return None
        return float(quote) if quote is not None else None

    @staticmethod
    def nifty_option_symbol(expiry: date, strike: int, direction: str) -> str:
        month_code = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7",
                      8: "8", 9: "9", 10: "O", 11: "N", 12: "D"}
        return f"NIFTY{expiry.year % 100:02d}{month_code[expiry.month]}{expiry.day:02d}{strike}{direction}"
