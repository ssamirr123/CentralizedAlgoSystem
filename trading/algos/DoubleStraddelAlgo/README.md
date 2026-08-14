# Nifty Hedged Double Straddle

Production-ready intraday NIFTY options-selling strategy with hedge protection,
built on Angel One SmartAPI. Two short straddles per day, protected by far-OTM
hedges bought first.

## Behaviour

| Time  | Action |
|-------|--------|
| 10:15 | BUY hedge CE + PE (LIMIT). Distance = 500 pts on expiry day, else 1000 pts. |
| 10:25 | SELL ATM CE + PE (LIMIT). Per leg: SL = entry+25, Target = entry-50. |
| 14:14 | Square off morning shorts + cancel morning pending orders. **Hedge stays active.** |
| 14:16 | SELL fresh ATM CE + PE (LIMIT). Same SL/target. |
| 15:25 | Square off all shorts + hedge, cancel all pending, stop. |

Any time: if day MTM <= `-DAILY_MAX_LOSS` -> emergency square-off (market) + stop.

## Run

```bash
cd hedged_double_straddle
pip install -r requirements.txt
python main.py
```

Edit credentials, lot size, timings, risk limits and the **expiry calendar**
(`EXPIRY_DATES`) in `config.py` first.

## Layout

```
main.py                 entry point + startup
config.py               all tunables + shared runtime state
connectapi.py           SmartAPI login (TOTP)
token_file.py           scrip master + strike -> (symbol, token) + nearest expiry
websocket_feed.py       live LTP feed (auto-reconnect) + REST fallback
broker/orders.py        LIMIT/market orders, retry, pending mgmt, reconciliation
strategy/expiry.py      is_expiry_day(), get_hedge_strikes()
strategy/hedge.py       hedge entry/exit + ATM
strategy/straddle.py    per-leg SL/target monitoring
strategy/engine.py      1-second scheduler wiring all sessions
risk/guard.py           daily max-loss kill switch + emergency square-off
state/store.py          JSON trade-state persistence (crash recovery)
report/csv_report.py    end-of-day CSV report
db/schema.sql           SQLite/Postgres trade schema
log_setup.py            stdout/stderr -> logs/<date>/app.log + Telegram
monitor.py              heartbeat/metrics glue (best-effort)
strategy_agent/agent.py central monitoring heartbeat client
```

## Notes / verify before live
- Confirm the current NIFTY **lot size** (`LOT_QTY`) and **strike step** (50).
- Populate `EXPIRY_DATES` from the official NSE calendar (fallback = Thursday).
- The 1-minute requirement is met by polling LTP each second and acting on
  1-minute-grade moves; gate `_monitor_leg` on `second == 0` if you want strict
  candle-close evaluation.

