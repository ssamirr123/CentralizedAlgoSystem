# EMA20_Pullback — intraday NIFTY option buyer (PAPER)

Buys 1-ITM NIFTY weekly CE/PE on a 5-min trend-pullback.

| Rule | Value |
|---|---|
| Trend filter | 5-min EMA20 vs EMA50 defines regime (CE only in uptrend, PE only in downtrend) |
| Entry (CE) | Prior bar low touches EMA20 (within 0.1%); current bar closes above prior high |
| Entry (PE) | Prior bar high touches EMA20; current bar closes below prior low |
| Scan window | 09:45 – 13:30 IST |
| Strike | 1-ITM (CE: ATM-50, PE: ATM+50) |
| Hard SL | -30% of premium |
| Target1 | +40%, books 50% |
| Trail (post-T1) | 5-min spot EMA10 — exit remainder on close through EMA10 against the trade |
| Hard exit | 14:30 IST |
| Max losses/day | 2 → signal disabled for the rest of the day |
| Weekly circuit-breaker | Equity ≤ -3% WTD → entries halted until Monday |
| Expiry-day guard | Thursday weekly expiry skipped |

Backtest 2021-01 → 2026-06 at ₹2L capital, 5 lots × 75, ₹30/leg:
**CAGR 76% · MaxDD -16% · Sharpe 2.03 · 1055 trades · WR 55% · 82% positive months.**

**Paper only** — no broker order is placed. Fills simulated at option LTP
from the broker-quote adapter. Ledger persists in
`trading/data/EMA20_Pullback.portfolio.json`.

## Operational config (env)

| Variable | Default | Purpose |
|---|---|---|
| `EMA20_PULLBACK_MODE` | `paper` | Only `paper` is implemented |
| `EMA20_PULLBACK_CAPITAL` | `200000` | Starting cash (INR) |
| `EMA20_PULLBACK_LOT_SIZE` | `75` | NIFTY contract lot |
| `EMA20_PULLBACK_LOTS` | `5` | Lots per trade |
| `EMA20_PULLBACK_BROKERAGE` | `30` | Per-leg brokerage |
| `EMA20_PULLBACK_COST_PCT` | `0.0007` | Turnover-based tax approximation |
| `EMA20_PULLBACK_MARKET_OPEN` | `09:15` | Session start (IST) |
| `EMA20_PULLBACK_MARKET_CLOSE` | `15:30` | Session end (IST) |
| `EMA20_PULLBACK_LOOP_INTERVAL` | `15` | Tick cadence (seconds) |

## Running

```bash
export EMA20_PULLBACK_CAPITAL=200000
export EMA20_PULLBACK_LOTS=5
python trading/algos/EMA20_Pullback/main.py
```
