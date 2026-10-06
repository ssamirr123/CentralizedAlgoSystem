"""EMA20_Pullback — intraday NIFTY option-buyer paper algo (single signal).

Buys 1-ITM CE/PE on a trend-pullback. In an uptrend (5-min EMA20 > EMA50),
when the prior bar's low touches EMA20 and the current bar closes above
the prior bar's high → CE. Mirror → PE.

Backtest (2021-01 to 2026-06, 5 lots x 75, Rs. 30/leg, Rs. 2L capital):
  CAGR 76%, MaxDD -16%, Sharpe 2.03, 1055 trades, WR 55%, 82% positive months.
"""
