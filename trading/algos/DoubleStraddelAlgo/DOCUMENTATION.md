# Nifty Hedged Double Straddle — Technical Documentation

> Production-ready intraday NIFTY options-selling bot built on the **Angel One SmartAPI**.
> It sells **two short straddles per day** (morning + afternoon), each protected by a
> far-OTM **hedge** bought first, with per-leg SL/target monitoring, a daily
> max-loss kill switch, crash recovery, and full logging.

---

## Table of Contents
1. [Overview](#1-overview)
2. [Strategy Logic](#2-strategy-logic)
3. [Architecture & Module Map](#3-architecture--module-map)
4. [Execution Flow (Step by Step)](#4-execution-flow-step-by-step)
5. [Configuration Reference](#5-configuration-reference)
6. [Runtime State & Data Files](#6-runtime-state--data-files)
7. [Risk Management](#7-risk-management)
8. [Order Execution Layer](#8-order-execution-layer)
9. [Crash Recovery](#9-crash-recovery)
10. [Logging, Monitoring & Reporting](#10-logging-monitoring--reporting)
11. [Dry-Run (Paper Trading) Mode](#11-dry-run-paper-trading-mode)
12. [Installation & Running](#12-installation--running)
13. [Database Schema](#13-database-schema)
14. [Operational Checklist](#14-operational-checklist)
15. [FAQ / Troubleshooting](#15-faq--troubleshooting)

---

## 1. Overview

| Property | Value |
|----------|-------|
| Instrument | NIFTY index options (NFO) |
| Broker / API | Angel One SmartAPI (`smartapi-python`) |
| Data feed | SmartWebSocketV2 (live LTP), REST fallback |
| Strategy type | Intraday short straddle (delta-neutral premium selling) with protective hedge |
| Sessions per day | 2 (morning + afternoon) |
| Runtime | Single Python process; blocks until 15:25 IST or emergency stop |

The whole system communicates via `print()`, which `log_setup` mirrors to a dated
log file (and optionally Telegram).

---

## 2. Strategy Logic

Times are IST (24h), defined in `config.py`.

| Time  | Action |
|-------|--------|
| **10:15** | **BUY hedge** CE + PE (LIMIT). Distance = **500 pts** on expiry day, else **1000 pts**. |
| **10:25** | **SELL morning straddle**: ATM CE + ATM PE (LIMIT). Per leg: **SL = entry + 25**, **Target = entry − 50**. |
| **14:14** | Square off morning shorts + cancel morning pending orders. **Hedge stays active.** |
| **14:16** | **SELL afternoon straddle**: fresh ATM CE + PE (LIMIT). Same SL/Target. |
| **15:25** | Square off all shorts + hedge, cancel all pending, write report, **stop**. |
| **Any time** | If day MTM ≤ `−DAILY_MAX_LOSS` → **emergency square-off** (market orders) + stop. |

**Key design points**
- The **hedge is bought first** (before selling naked options) to cap tail risk.
- Each straddle leg is monitored **independently** on its own thread; if one leg
  hits SL/Target, the other continues.
- All entries/exits are **LIMIT** orders. **MARKET** orders are used *only* on the
  emergency square-off path.

---

## 3. Architecture & Module Map

```
main.py                  Entry point + startup sequence
config.py                All tunables + shared runtime state (objconn, ltp, kill_switch...)
connectapi.py            SmartAPI login via TOTP (retries, returns None on failure)
token_file.py            Downloads scrip master; strike -> (symbol, token); nearest expiry
websocket_feed.py        Live LTP feed (auto-reconnect) + REST fallback + wait_ltp

broker/
  orders.py              LIMIT/MARKET orders, retry, pending mgmt, reconciliation, cancels

strategy/
  expiry.py              is_expiry_day(), get_hedge_strikes(), symbol-based expiry parsing
  hedge.py               Hedge entry/exit + get_atm()
  straddle.py            Straddle entry/exit + per-leg SL/target monitor threads
  engine.py              1-second scheduler wiring all timed sessions together

risk/
  guard.py               Daily max-loss kill switch + emergency square-off + reconciliation

state/
  store.py               Atomic JSON trade-state persistence (crash recovery)

report/
  csv_report.py          End-of-day CSV trade report

db/
  schema.sql             SQLite/PostgreSQL trade schema (sessions, trades, orders_audit)

log_setup.py             stdout/stderr -> logs/<date>/app.log + optional Telegram
monitor.py               Heartbeat/metrics glue (best-effort, currently disabled)
strategy_agent/agent.py  Central monitoring heartbeat client
```

### Data flow (high level)

```
                +-------------------+
   TOTP login   |   connectapi      |
   ------------>|  makeconnection   |---> config.objconn (SmartConnect)
                +-------------------+
                          |
   scrip master  +--------v---------+     live ticks   +------------------+
   ------------->|   token_file     |                  |  websocket_feed  |
   nifty_token.csv|  token_nifty()  |<---subscribe()---|  config.ltp{}    |
                +--------+---------+                  +---------+--------+
                          |                                     |
                +---------v-----------------------------------v--+
                |                strategy/engine                 |
                |  10:15 hedge  10:25 morn  14:14 exit  14:16 aft |
                |  15:25 final                                    |
                +----+----------------+-----------------+---------+
                     |                |                 |
               strategy/hedge   strategy/straddle   broker/orders
                     |                |                 |
                     +--------> state/store <-----------+
                                (state.json)
                     risk/guard  (parallel daemon: MTM kill switch)
                     report/csv_report (EOD)
```

---

## 4. Execution Flow (Step by Step)

### Phase 1 — Startup (`main.py`, runs immediately on launch)
1. `log_setup.init()` — redirect stdout/stderr to `logs/<date>/app.log` (+ Telegram if configured). **Must be the first import.**
2. `monitor.start()` — start the heartbeat agent (no-op while `monitoring_enabled = False`).
3. `token_file.download_token()` — download NFO scrip master, filter NIFTY `OPTIDX` rows → save `nifty_token.csv`.
4. `connectapi.makeconnection()` — TOTP login. `main` **retries every 5 s** until a valid session exists (never runs with `objconn = None`).
5. Start `websocket_feed.connect()` on a **daemon thread** (auto-reconnecting), sleep 3 s for the socket to open.
6. Call `engine.run()` — **blocks until 15:25 or emergency stop**.

### Phase 2 — Engine init (`strategy/engine.py → run()`)
1. `state = load()` — recover `state.json` (or fresh skeleton).
2. `guard.start_guard(state)` — launch the risk daemon thread.
3. Mark already-completed steps as `done` and re-attach monitor threads (`resume_monitors`) for legs open at crash time.
4. Enter a **1-second loop** using `_hit(now, hhmm)` to fire each timed action exactly once.

### Phase 3 — The trading day
| Trigger | Function | What happens |
|---------|----------|--------------|
| 10:15 | `hedge.enter_hedge` | Compute ATM, detect expiry, pick hedge strikes (±500/±1000), BUY LIMIT CE+PE, subscribe, save state |
| 10:25 | `straddle.enter_straddle('morning')` | Compute ATM, SELL LIMIT CE+PE, `wait_ltp` for entry prices, spawn per-leg monitor threads |
| 14:14 | `straddle.time_exit_straddle('morning')` | BUY back open morning legs (Time Exit), cancel *morning-only* pending orders. Hedge untouched |
| 14:16 | `straddle.enter_straddle('afternoon')` | Same as morning entry with a fresh ATM |
| 15:25 | final block | Exit afternoon shorts + `exit_hedge` + `cancel_all_pending` + `write_report`, then **break** |

### Phase 4 — Per-leg monitor (`_monitor_leg`, one thread per leg)
- Establishes a real `entry` price (via `wait_ltp` on recovery).
- Every second, reads live LTP:
  - `ltp >= entry + SL_POINTS (25)` → `_close_leg(reason='SL')`
  - `ltp <= entry − TARGET_POINTS (50)` → `_close_leg(reason='Target')`
- Stops on `leg['done']` or `config.kill_switch`.

### Phase 5 — Risk daemon (`risk/guard.py`, parallel, every 120 s)
- Recompute day MTM from the broker position book.
- If MTM ≤ `−DAILY_MAX_LOSS` → set `kill_switch`, `emergency_square_off` (MARKET), cancel all, return.
- Else refresh the order book (reconciliation).

---

## 5. Configuration Reference

All settings live in `config.py`.

### Run mode
| Key | Default | Meaning |
|-----|---------|---------|
| `DRY_RUN` | `false` | Paper trading. Enable via env `BOT_DRY_RUN=true` (alias `BOT_DY_RUN`). |

### Credentials (⚠️ replace before live)
`clientid`, `apikey`, `mpin`, `token` (TOTP secret).

### Instrument
| Key | Default |
|-----|---------|
| `INDEX_NAME` | `NIFTY` |
| `INDEX_TOKEN` | `99926000` (NIFTY spot, NSE) |
| `INDEX_EXCH` / `OPT_EXCH` | `NSE` / `NFO` |
| `STRIKE_STEP` | `50` |
| `LOT_QTY` | `65` (verify current NIFTY lot size) |

### Timings (IST)
`HEDGE_ENTRY=(10,15)`, `MORNING_ENTRY=(10,25)`, `MORNING_EXIT=(14,14)`,
`AFT_ENTRY=(14,16)`, `FINAL_EXIT=(15,25)`.

### SL / Target / Hedge distance
`SL_POINTS=25`, `TARGET_POINTS=50`, `HEDGE_GAP_EXPIRY=500`, `HEDGE_GAP_NONEXPIRY=1000`.

### Order / retry
| Key | Default | Meaning |
|-----|---------|---------|
| `ORDER_MAX_RETRIES` | `5` | Max placement attempts |
| `ORDER_RETRY_DELAY` | `2` s | Delay between retries |
| `LIMIT_SLIPPAGE` | `0.50` | Rupees to walk limit toward market per retry |
| `PENDING_TIMEOUT` | `5` s | How long a limit may stay pending before action |
| `PENDING_ACTION` | `MODIFY` | `MODIFY` \| `CANCEL` \| `MARKET` once timeout hit |
| `ALLOW_MARKET_EMERGENCY` | `True` | Market orders allowed only in emergency |

### Risk
`DAILY_MAX_LOSS=15000.0`, `RECONCILE_INTERVAL=120` s.

### Expiry calendar
- `EXPIRY_DATES` — explicit override list (`YYYY-MM-DD`). Highest priority.
- `EXPIRY_WEEKDAY=1` — fallback weekday (Tue=1).
- `HOLIDAYS` — NSE holidays; expiry shifts back to previous trading day.

### Monitoring / Telegram
`monitoring_enabled=False`, `strategy_name`, `server_name`, `api_base_url`,
`telegram_enabled`, `telegram_bot_token`, `telegram_chat_id`.

### Shared runtime state (do not edit manually)
`objconn`, `sws`, `ltp{}`, `orderbook[]`, `positions[]`, `kill_switch`.

---

## 6. Runtime State & Data Files

| File | Produced by | Purpose |
|------|-------------|---------|
| `nifty_token.csv` | `token_file.download_token()` | Cached option chain (symbol/token/expiry) |
| `state.json` | `state/store.py` | Atomic trade state for crash recovery |
| `trades_<YYYY-MM-DD>.csv` | `report/csv_report.py` | End-of-day P&L report |
| `logs/<YYYY-MM-DD>/app.log` | `log_setup.py` | Full console mirror, rolls at midnight |

### `state.json` shape (illustrative)
```json
{
  "hedge_active": true,
  "hedge": {
    "ce": {"sym": "...", "tok": "...", "strike": 25500, "oid": "...", "entry": 12.5, "exit": 8.0},
    "pe": {"sym": "...", "tok": "...", "strike": 24500, "oid": "...", "entry": 11.0}
  },
  "morning": {
    "entered": true, "atm": 25000,
    "ce": {"sym": "...", "tok": "...", "entry": 120.0, "done": true, "exit": 145.0, "exit_reason": "SL"},
    "pe": {"sym": "...", "tok": "...", "entry": 115.0, "done": false}
  },
  "afternoon": { "...": "..." }
}
```

---

## 7. Risk Management

- **Daily max-loss kill switch** — background loop recomputes day MTM every
  `RECONCILE_INTERVAL` seconds. When MTM ≤ `−DAILY_MAX_LOSS`, it trips
  `config.kill_switch` and calls `emergency_square_off`.
- **Emergency square-off** — flattens all short legs (both sessions) and the hedge
  using **MARKET** orders, cancels all pending orders, and stops trading for the day.
- **Reconciliation** — the same daemon periodically refreshes the broker order book
  so cached state stays consistent with the broker.

---

## 8. Order Execution Layer (`broker/orders.py`)

- **`place_limit`** — LIMIT order with retry. `_limit_price` walks the price toward
  the market by `LIMIT_SLIPPAGE × attempt` each retry (BUY adds, SELL subtracts,
  floored at 0.05). Prices are rounded to the **0.05 NFO tick**. After placement, a
  background thread (`_manage_pending`) runs so placement returns immediately (keeping
  the two straddle legs effectively simultaneous).
- **`place_market`** — emergency-only MARKET order; degrades to LIMIT if
  `ALLOW_MARKET_EMERGENCY` is `False`.
- **`_manage_pending`** — after `PENDING_TIMEOUT`, refreshes the order book, detects
  **partial fills**, and applies `PENDING_ACTION` (`MODIFY` / `CANCEL` / `MARKET`) to
  the unfilled remainder only.
- **`refresh_orderbook` / `refresh_positions`** — cached reconciliation with retries.
- **`cancel_all_pending`** — cancels every open/pending order (used at 15:25).
- **`cancel_pending_for_tokens`** — cancels only specific tokens' pending orders
  (used at 14:14 so the **hedge is never touched**).

---

## 9. Crash Recovery

State is persisted atomically after every meaningful change (`state/store.py`,
temp file + `os.replace`), stamped with the trading day (`date`, `YYYY-MM-DD`).
On restart mid-day, `engine.run()`:
- loads `state.json`,
- if the stored `date` isn't today, the file is **stale** (from a prior day) and
  is discarded — a fresh skeleton is recreated so the strategy never resumes
  yesterday's symbols/tokens/entries,
- otherwise, **skips** any step already completed (`hedge_active`, `morning.entered`,
  `afternoon.entered`) — prevents duplicate entries,
- **re-attaches** monitor threads (`straddle.resume_monitors`) for any leg that was
  open (`entered` but not `done`), re-subscribing its token to the live feed.

`_monitor_leg` also re-establishes an `entry` price via `wait_ltp` if it is missing,
so SL/target are never computed against a bogus `0`.

---

## 10. Logging, Monitoring & Reporting

- **Logging (`log_setup.py`)** — a `_Tee` wraps stdout/stderr, mirroring every
  `print()` to `logs/<date>/app.log` (path resolved per write → auto rollover at
  midnight). Optional **Telegram** forwarding batches lines on a daemon thread
  (never blocks trading).
- **Monitoring (`monitor.py` + `strategy_agent/agent.py`)** — best-effort heartbeat
  client that POSTs `{mtm, pnl, trade_count, status}` to a central server every 30 s.
  Fully wrapped in try/except so it can never affect trading. **Disabled** by default
  (`monitoring_enabled = False`).
- **Report (`report/csv_report.py`)** — at 15:25 (and on exit) writes
  `trades_<date>.csv` with columns: `date, leg_type, session, symbol, strike,
  option_type, side, qty, entry_price, exit_price, exit_reason, pnl_points,
  pnl_amount`, plus an estimated total P&L and the broker day MTM in the log.

---

## 11. Dry-Run (Paper Trading) Mode

Enable with:
```bash
export BOT_DRY_RUN=true
python main.py
```
Behaviour when `DRY_RUN` is on:
- `place_limit` / `place_market` / `modify_limit` / `cancel` **only log**
  `[DRY RUN] ...` and return simulated order IDs (`DRYRUN-<ms>`) — nothing reaches
  the broker.
- `refresh_orderbook` / `refresh_positions` skip the real API and return cached
  locals.
- All timings, ATM/hedge selection, SL/target logic, state persistence, and
  reporting run identically — so you get a realistic paper-trading dry run.

> Note: login and the WebSocket feed still connect in dry-run so live prices drive
> the simulated logic. Ensure valid credentials in `config.py`.

---

## 12. Installation & Running

```bash
cd hedged_double_straddle
python -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt

# 1) Edit config.py: credentials, LOT_QTY, timings, DAILY_MAX_LOSS, EXPIRY_DATES.
# 2) (Optional) paper trade first:
export BOT_DRY_RUN=true

python main.py
```

**Dependencies** (`requirements.txt`): `smartapi-python`, `pyotp`, `pandas`,
`numpy`, `requests`.

---

## 13. Database Schema

`db/schema.sql` defines a SQLite/PostgreSQL schema (not yet wired into the code —
the live path uses `state.json` + CSV). Three tables:

- **`sessions`** — one row per trading day: `trade_date`, `is_expiry_day`,
  `hedge_gap`, `day_mtm`, `day_pnl`, `kill_switch`.
- **`trades`** — one row per leg: `leg_type` (`HEDGE`/`SHORT`), `strategy`
  (`HEDGE`/`MORNING`/`AFTERNOON`), symbol, strike, option type, side, entry/exit
  order IDs, prices, times, `exit_reason`
  (`SL`/`Target`/`Time Exit`/`Emergency Exit`/`Day Close`), P&L fields.
- **`orders_audit`** — every order action: `PLACE/MODIFY/CANCEL/RETRY/PARTIAL/REJECT`,
  order type, price, qty, filled qty, status, attempt, timestamp.

Indexes on `trades(trade_date)` and `orders_audit(trade_date)`.

---

## 14. Operational Checklist

Before going live:
- [ ] Replace **credentials** in `config.py` (`clientid`, `apikey`, `mpin`, `token`).
- [ ] Confirm current NIFTY **lot size** (`LOT_QTY`) and **strike step** (`STRIKE_STEP`).
- [ ] Populate **`EXPIRY_DATES`** from the official NSE calendar; update `HOLIDAYS`.
- [ ] Verify `DAILY_MAX_LOSS` matches your risk appetite.
- [ ] Run a full day in **`BOT_DRY_RUN=true`** and review `trades_<date>.csv` + logs.
- [ ] Ensure the server clock is set to **IST** (timings use local `datetime.now()`).
- [ ] Confirm sufficient margin for 4 short legs + 4 hedge legs.

---

## 15. FAQ / Troubleshooting

**Q: The bot logs `No broker session yet - retrying`.**
Login failed. Check `apikey`/`clientid`/`mpin`/TOTP `token` and that SmartAPI access is active.

**Q: SL/Target never triggers.**
Confirm the option token is subscribed and ticks are arriving (`[WS] subscribed ...`).
`_monitor_leg` needs a valid `entry` price from `wait_ltp`.

**Q: Orders stay pending.**
Tune `PENDING_TIMEOUT`, `PENDING_ACTION`, and `LIMIT_SLIPPAGE`. `MODIFY` re-prices the
limit toward market each retry.

**Q: How do I evaluate strictly on 1-minute candle closes?**
Gate `_monitor_leg` on `datetime.now().second == 0`; currently it polls each second and
acts on 1-minute-grade moves.

**Q: It stopped mid-day — can I restart safely?**
Yes. State is persisted; on restart, completed steps are skipped and open legs resume
monitoring (no duplicate entries).

**Q: Timezone?**
All timings use the server's local clock via `datetime.now()`. Run on an IST-configured
host.

---

*Generated documentation. Cross-check with `config.py` for the authoritative,
current parameter values before every trading session.*

