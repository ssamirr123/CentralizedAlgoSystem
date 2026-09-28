"""
indicators.py
Pivot Points (R1, S1) and Supertrend (7, 3) computations.
"""

import pandas as pd

import config


def daily_pivots(daily_df):
    """
    Calculate classic floor-trader pivot points from the previous day's candle.

    Parameters
    ----------
    daily_df : pandas.DataFrame
        Must contain previous day's high, low, close. The last row is assumed
        to be the most recent *completed* day.

    Returns
    -------
    dict with keys: pivot, r1, s1, r2, s2
    """
    prev = daily_df.iloc[-1]
    high, low, close = prev["high"], prev["low"], prev["close"]

    pivot = (high + low + close) / 3.0
    r1 = (2 * pivot) - low
    s1 = (2 * pivot) - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)

    return {"pivot": pivot, "r1": r1, "s1": s1, "r2": r2, "s2": s2}


def previous_day_ohlc(intraday_5m_df):
    """
    Collapse a 5-minute intraday DataFrame into daily OHLC rows so we can derive
    the previous trading day's High/Low/Close for pivots.
    """
    df = intraday_5m_df.copy()
    df["date"] = df["datetime"].dt.date
    daily = df.groupby("date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).reset_index()
    return daily


def supertrend(df, period=None, multiplier=None):
    """
    Compute the Supertrend indicator without external TA dependencies.

    Parameters
    ----------
    df : pandas.DataFrame with columns high, low, close
    period : int   -> ATR period (default from config)
    multiplier : float -> ATR multiplier (default from config)

    Returns
    -------
    pandas.DataFrame copy with added columns:
        'supertrend'      -> the supertrend line value
        'supertrend_dir'  -> "GREEN" (bullish) or "RED" (bearish)
    """
    period = period or config.SUPERTREND_PERIOD
    multiplier = multiplier or config.SUPERTREND_MULTIPLIER

    data = df.copy().reset_index(drop=True)
    high = data["high"]
    low = data["low"]
    close = data["close"]

    # True Range and ATR (Wilder's smoothing).
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()

    hl2 = (high + low) / 2.0
    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr

    final_upper = upper_band.copy()
    final_lower = lower_band.copy()

    for i in range(1, len(data)):
        if close[i - 1] <= final_upper[i - 1]:
            final_upper[i] = min(upper_band[i], final_upper[i - 1])
        else:
            final_upper[i] = upper_band[i]

        if close[i - 1] >= final_lower[i - 1]:
            final_lower[i] = max(lower_band[i], final_lower[i - 1])
        else:
            final_lower[i] = lower_band[i]

    st = pd.Series(index=data.index, dtype="float64")
    direction = pd.Series(index=data.index, dtype="object")

    # Initialize first row.
    st.iloc[0] = final_upper.iloc[0]
    direction.iloc[0] = "RED"

    for i in range(1, len(data)):
        if st.iloc[i - 1] == final_upper.iloc[i - 1]:
            if close[i] <= final_upper[i]:
                st.iloc[i] = final_upper[i]
                direction.iloc[i] = "RED"
            else:
                st.iloc[i] = final_lower[i]
                direction.iloc[i] = "GREEN"
        else:
            if close[i] >= final_lower[i]:
                st.iloc[i] = final_lower[i]
                direction.iloc[i] = "GREEN"
            else:
                st.iloc[i] = final_upper[i]
                direction.iloc[i] = "RED"

    data["supertrend"] = st
    data["supertrend_dir"] = direction
    return data

