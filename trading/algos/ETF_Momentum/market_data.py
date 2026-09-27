"""
Daily closes from yfinance (<SYMBOL>.NS), the same source and adjustment
as the backtest. Imported lazily so the strategy logic and its tests never
need yfinance or the network.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

_logger = logging.getLogger("trading.ETF_Momentum.market_data")


def _download_one(symbol: str, start: date, end: date) -> pd.Series | None:
    import yfinance as yf

    try:
        df = yf.download(f"{symbol}.NS", start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                         auto_adjust=True, progress=False, threads=False)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("yfinance download failed for %s: %s", symbol, exc)
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Close" not in df.columns:
        return None
    s = df["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = pd.to_numeric(s, errors="coerce").dropna()
    s = s[s > 0]
    idx = pd.to_datetime(s.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    s.index = idx.normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s if not s.empty else None


def fetch_closes(symbols: list[str], start: date, end: date) -> tuple[pd.DataFrame, list[str]]:
    """(closes, missing). Union calendar of all symbols, forward-filled only."""
    series, missing = {}, []
    for sym in symbols:
        s = _download_one(sym, start, end)
        if s is None:
            missing.append(sym)
        else:
            series[sym] = s
    if not series:
        return pd.DataFrame(), missing
    closes = pd.DataFrame(series).sort_index()
    closes = closes[closes.index <= pd.Timestamp(end)].ffill()
    return closes, missing
