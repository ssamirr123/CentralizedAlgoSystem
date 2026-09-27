# ETF_Momentum — monthly ETF momentum rotation (PAPER)

Holds the top 6 NSE ETFs (C54 universe) by blended momentum, rebalanced on
the first trading day of each month at the close. Same rules as the
backtest `ETF_Momentum_Test_1.py` (2021-01 → 2026-08: CAGR 29.5%, daily
max drawdown -25%, before costs and tax).

**Paper only.** No broker is connected; fills are simulated at the
rebalance-day close and kept in `trading/data/ETF_Momentum.portfolio.json`
(git-ignored, survives restarts and `git pull`). `ETF_MOMENTUM_MODE` other
than `paper` refuses to start.

## Rules (fixed in `config.py`)

| Rule | Value |
|---|---|
| Score | 0.15×1M + 0.40×3M + 0.30×6M + 0.15×12M return (21/63/126/252 trading days) |
| Holdings | top 6, new entries sized at 1/6 of portfolio value, whole units |
| Entry gate | close > 200-day SMA; failed slot stays empty (no #7 substitute) |
| Exits (priority) | hard SL: close ≤ entry×0.85 → trail: close ≤ peak×0.80 → rotation: out of top 6 |
| Idle cash | 5% p.a. simple, accrued at rebalances |

Stops and peaks are evaluated only on rebalance days, exactly as in the
backtest.

## Schedule

- Runs continuously; heartbeat every 10 s, marks/positions/day P&L
  refreshed every 15 min in market hours (yfinance daily bars).
- The rebalance always uses the **first trading day's close** (the
  backtest's price). Today's bar only counts as a close after
  `ETF_MOMENTUM_CLOSE_FINAL_TIME` IST (default 16:00).
  - On the EventBridge schedule (algos run 09:00–15:27 IST) the algo is
    down at the close, so it rebalances the **next morning** with the
    first trading day's close. Trades are dated that day.
  - On a box running 24/7 it rebalances the same evening after 16:00.
  - Holidays are handled by the data: the first session in the month's
    price data is the first trading day.
- Missed it entirely (algo not running the day after either)? It
  rebalances late at the latest close and ships a WARNING to the Logs page.
- A fresh ledger waits for the next month's first trading day unless
  `ETF_MOMENTUM_START_NOW=true`.

## Dashboard

- **Algorithms / Heartbeats:** Mode `PAPER`, Running lots = number of ETFs
  held, P&L = today's P&L of the holdings.
- **Positions:** one row per ETF with unrealised P&L since entry.
- **Trades:** every paper fill (`order_id` `PAPER-YYYYMMDD-SIDE-SYMBOL`).
- **Logs:** `ENTRY` / `EXIT` / `SL` events per fill.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `ETF_MOMENTUM_MODE` | `paper` | only `paper` is supported |
| `ETF_MOMENTUM_CAPITAL` | `1000000` | starting capital (₹) for a new ledger |
| `ETF_MOMENTUM_CLOSE_FINAL_TIME` | `16:00` | IST time after which today's bar is treated as the final close |
| `ETF_MOMENTUM_START_NOW` | `false` | new ledger rebalances at the latest close instead of waiting for next month |

`STRATEGY_NAME` / `SERVER_NAME` / `API_BASE_URL` / `CONTROL_API_KEY` are
injected by `START_ALGO` as for every algo.

To reset the paper run, stop the algo and delete
`trading/data/ETF_Momentum.portfolio.json`.

## Run locally

```bash
pip install -r trading/algos/ETF_Momentum/requirements.txt
ETF_MOMENTUM_START_NOW=true python trading/algos/ETF_Momentum/main.py
```
