"""VWAP_Reclaim — intraday NIFTY option-buyer paper algo (single signal).

Buys 1-ITM CE/PE when 5-min NIFTY spot reclaims cumulative VWAP after
having been below it (CE), or rejects it from above (PE).

Backtest (2021-01 to 2026-06, 5 lots x 75, Rs. 30/leg, Rs. 2L capital):
  CAGR 76%, MaxDD -27%, Sharpe 1.74, 959 trades, WR 62%, 89% positive months.
"""
