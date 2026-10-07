# VWAP_Reclaim — intraday NIFTY option buyer (PAPER)

Buys 1-ITM NIFTY weekly CE/PE on a 5-min VWAP reclaim.

| Rule | Value |
|---|---|
| Entry (CE) | Price was below cumulative VWAP; a 5-min bar opens below VWAP and closes above |
| Entry (PE) | Price was above VWAP; a 5-min bar opens above and closes below |
| Scan window | 09:30 – 13:00 IST |
| Strike | 1-ITM (CE: ATM-50, PE: ATM+50) |
| Hard SL | -30% of premium |
| Target1 | +30%, books 50% |
| Trail (post-T1) | 5-min spot EMA10 — exit remainder on close through EMA10 against the trade |
| Hard exit | 14:30 IST |
| Max losses/day | 2 → signal disabled for the rest of the day |
| Weekly circuit-breaker | Equity ≤ -3% WTD → entries halted until Monday |
| Expiry-day guard | Thursday weekly expiry skipped |

Backtest 2021-01 → 2026-06 at ₹2L capital, 5 lots × 75, ₹30/leg:
**CAGR 76% · MaxDD -27% · Sharpe 1.74 · 959 trades · WR 62% · 89% positive months.**

**Paper only** — no broker order is placed. Fills simulated at option LTP
from the broker-quote adapter. Ledger persists in
`trading/data/VWAP_Reclaim.portfolio.json`.

## Operational config (env)

| Variable | Default | Purpose |
|---|---|---|
| `VWAP_RECLAIM_MODE` | `paper` | Only `paper` is implemented |
| `VWAP_RECLAIM_CAPITAL` | `200000` | Starting cash (INR) |
| `VWAP_RECLAIM_LOT_SIZE` | `75` | NIFTY contract lot |
| `VWAP_RECLAIM_LOTS` | `5` | Lots per trade |
| `VWAP_RECLAIM_BROKERAGE` | `30` | Per-leg brokerage |
| `VWAP_RECLAIM_COST_PCT` | `0.0007` | Turnover-based tax approximation |
| `VWAP_RECLAIM_MARKET_OPEN` | `09:15` | Session start (IST) |
| `VWAP_RECLAIM_MARKET_CLOSE` | `15:30` | Session end (IST) |
| `VWAP_RECLAIM_LOOP_INTERVAL` | `15` | Tick cadence (seconds) |

## Running

```bash
export VWAP_RECLAIM_CAPITAL=200000
export VWAP_RECLAIM_LOTS=5
python trading/algos/VWAP_Reclaim/main.py
```
