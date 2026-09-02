# Market Data Engine (Stage 19 — ICICI Breeze)

A centralized, provider-abstracted market-data service inside the existing
FastAPI backend. Collects NIFTY 50 / BANKNIFTY / INDIA VIX / SENSEX and
the NIFTY option chain during the Indian market session and exposes them
to the React dashboard and (later) to strategies.

```
09:10 IST  start   ·   15:45 IST  stop   ·   TZ Asia/Kolkata
```

Paper trading only. **No order-placement code exists in this engine.**

---

## 1. Architecture

```
ICICI BREEZE
   │  API key + secret + DAILY session token
   ▼
Breeze session manager  (trading/market_data/session.py)
   │  VALID / SESSION_REQUIRED / ERROR
   ▼
MarketDataProvider (providers/base.py) ── ICICIBreezeProvider (providers/icici_breeze.py)
   │  get_index_quote / get_option_chain / get_historical_candles / subscribe
   ▼
MarketDataService (service.py)  ◄── MarketDataScheduler (scheduler.py, 09:10/15:45 IST)
   │
   ├─► LiveCache (cache.py)            in-memory, stale detection
   ├─► CandleAggregator (aggregator.py)  tick → 1-minute OHLC → PostgreSQL
   └─► realtime publish (market_quote / market_status)  throttled

FastAPI  /api/market/*  (market_routes.py, VIEW)   +   WS /api/ws (reused)
   ▼
React: MarketTicker · IndexCard · NiftyOptionChain · MarketChart · MarketFeedStatus
```

Single process (same assumption as the realtime EventBus). Runs on the
existing backend EC2 — no new instance.

## 2. ICICI Breeze integration

* SDK: `breeze-connect` (in `requirements.txt`; **lazy-imported** so the
  package/tests still work without it).
* `BreezeConnect(api_key)` → `generate_session(api_secret, session_token)`.
* Reads: `get_quotes` (cash / options), `get_option_chain_quotes`,
  `get_historical_data_v2`, `ws_connect` + `subscribe_feeds` + `on_ticks`.
* Breeze codes are contained in `providers/icici_breeze.py` (`_INDEX_CODES`
  = `NIFTY / CNXBAN / INDVIX / BSESEN`). **Verify these against your Breeze
  account** — Bank Nifty and SENSEX (BSE entitlement) in particular.

## 3. Authentication / session handling

Breeze has **no supported programmatic login** (web + OTP). The engine
never attempts one.

Credential resolution precedence: **runtime override → AWS Secrets
Manager (`BREEZE_SECRET_ID`) → environment (`BREEZE_*`, legacy
`ICICI_BREEZE_*` fallback)**.

Daily flow:
```
each trading morning:
  operator generates today's token via the Breeze login page
  POST /api/market/session { "session_token": "<token>" }   (ADMIN)
  -> stored in-process, re-validated, dashboard reflects VALID
```
The runtime override is **process-local** — a backend restart loses it.
For restart-survival also update the Secrets Manager secret or
`/etc/centralized-algo/backend.env`.

Session states: `NOT_CONFIGURED · SESSION_REQUIRED · VALID · ERROR · UNKNOWN`.
The token is never returned by any API and never logged (only a
`sha256[:12]` fingerprint).

## 4. Daily schedule

`scheduler.py` — timezone-aware, `zoneinfo("Asia/Kolkata")`. `decide(now)`
is pure and returns `START / STOP / RUNNING / IDLE`. Never uses naive
`datetime.now()`. Holidays: `MARKET_DATA_HOLIDAYS` (`YYYY-MM-DD,...`);
weekend + holiday aware (not "Mon-Fri == open").

Startup (09:10): verify trading day → verify Breeze session → connect →
subscribe indices → refresh instrument master + persist `option_contracts`
→ resolve NIFTY spot / ATM / expiries → subscribe ATM±N option universe →
start aggregation + persistence loop → mark **RUNNING only after a real
tick lands**.

Stop (15:45): stop subscriptions → stop accepting ticks → force-close the
current minute → flush pending DB writes → close Breeze socket → clear
cache → mark STOPPED + log daily stats. **No orders.**

## 5. Instrument discovery

`instruments.py::InstrumentMaster` + `ICICIBreezeProvider.get_option_instruments`
parse ICICI's **published** security master (NFO/FONSE segments — an ICICI
artifact, not NSE/BSE scraping). Contracts resolve dynamically; **no
token is hardcoded**. `needs_refresh(today)` gates the daily refresh.

Each contract carries: `underlying, expiry, strike, option_type,
provider_token, exchange, lot_size, tick_size, symbol` (internal symbol
`NIFTY|YYYY-MM-DD|STRIKE|CE`).

## 6. NIFTY option chain

`option_chain.build_option_chain(...)`:
* expiry: `current` (nearest) / `next` / a specific date.
* ATM = strike nearest the **live** NIFTY spot.
* strike step from the instrument master (consecutive-strike spacing), not
  hardcoded.
* window = ATM ± `NIFTY_OPTION_STRIKE_RANGE` (default 10; UI offers 5/10/20).
* per strike: CE/PE `ltp, open, high, low, prev_close, volume, oi,
  oi_change, bid, ask, iv, vwap`. **Missing fields are `null`, never
  fabricated.**

## 7. Strike selection

`select_strike_window(sorted_strikes, atm, n)` returns `atm-n … atm+n`.
Only the subscribed window is streamed — never "all strikes".

## 8. Live cache

`cache.py::LiveCache` — in-memory only (rule 17). `get_latest_quote(symbol)`
/ `get_option_quote(symbol)`. Tracks provider timestamp + local receive
time. `MARKET_DATA_STALE_SECONDS` (default 10) → `status == "stale"`.

## 9. 1-minute aggregation

`aggregator.py` — `tick → minute bucket → Candle`. `flush(now)` closes
every elapsed minute; **exactly one row per (symbol/contract, minute)**
reaches PostgreSQL. Late ticks for a closed minute are dropped.

## 10. PostgreSQL schema

Migration `323f5b753c2c` (down-revision `a1b2c3d4e5f6`):

| table | key columns | unique |
|---|---|---|
| `market_candles` | timestamp, symbol, exchange, interval, OHLC, volume, oi | (symbol, exchange, interval, timestamp) |
| `option_contracts` | underlying, exchange, provider, provider_token, symbol, expiry, strike, option_type, lot_size, tick_size | symbol · (provider, provider_token) · (underlying, exchange, expiry, strike, option_type) |
| `option_candles` | timestamp, contract_id (FK→option_contracts, ON DELETE CASCADE), OHLC, volume, oi | (contract_id, timestamp) |

Indexes on `(symbol,timestamp)`, `(underlying,expiry)`, `(contract_id,timestamp)`.

## 11. API endpoints (all `VIEW`)

```
GET  /api/market/indices
GET  /api/market/indices/{symbol}          alias-tolerant (nifty, vix, ...)
GET  /api/market/nifty/expiries
GET  /api/market/nifty/strikes?expiry=current|next|YYYY-MM-DD
GET  /api/market/nifty/option-chain?expiry=current&range=10
GET  /api/market/candles/{symbol}?interval=1minute&limit=375   (index or option symbol)
GET  /api/market/health
GET  /api/market/session/status
POST /api/market/session                    ADMIN only; token write-only
```

## 12. WebSocket

Reuses `/api/ws`. New event types `market_quote` (throttled to
`MARKET_WS_UPDATE_INTERVAL_MS`, default 1000 ms) and `market_status`
(feed/session transitions). Option-chain updates are not fanned out
tick-by-tick — the client resyncs the chain over REST on `market_quote`
of kind `option`.

## 13. React dashboard

`/market` route (VIEW). `MarketTicker` sits in the top bar always.
`MarketPage` = IndexCards + `MarketFeedStatus` (8 states + "Refresh Breeze
Session" for admins) + `MarketChart` (inline-SVG 1-minute line, no chart
lib) + `NiftyOptionChain` (expiry selector, ATM±5/10/20, ATM row
highlighted).

## 14. Error recovery

`service.py` — bounded exponential-backoff reconnect (1→30 s, ≤5 attempts,
`reconnect_count` tracked) triggered when the last tick ages past
`3 × MARKET_DATA_STALE_SECONDS` during market hours. A bad tick never
kills the socket thread. Session-expired → `SESSION_REQUIRED`, feed does
not run. DB write failure is logged (`market_data.database_error`) and
retried next flush.

## 15. Security

Breeze key / secret / session token are server-side only — never in React,
API responses, `localStorage`, logs, CloudWatch, or Git. Only booleans +
source + fingerprint leave the process. Stage 18 auth / JWT / refresh
cookie / CORS / RBAC / audit are unchanged. `POST /api/market/session`
requires ADMIN and writes an `MARKET_SESSION_UPDATED` audit row (fingerprint
only).

## 16. Configuration

All via `trading/core/config.py::Settings` (no second `.env` system):

```
MARKET_DATA_ENABLED=false          # opt-in; nothing contacts Breeze unless true
MARKET_DATA_PROVIDER=icici_breeze
MARKET_DATA_TIMEZONE=Asia/Kolkata
MARKET_DATA_START_TIME=09:10
MARKET_DATA_STOP_TIME=15:45
MARKET_DATA_HOLIDAYS=              # "2026-01-26,2026-08-15" optional
NIFTY_OPTION_STRIKE_RANGE=10
MARKET_DATA_STALE_SECONDS=10
MARKET_WS_UPDATE_INTERVAL_MS=1000
MARKET_DATA_RETENTION_DAYS=365
OPTION_DATA_RETENTION_DAYS=180
BREEZE_ENABLED=false
BREEZE_API_KEY=      (fallback ICICI_BREEZE_API_KEY)
BREEZE_SECRET_KEY=   (fallback ICICI_BREEZE_API_SECRET)
BREEZE_SESSION_TOKEN=(fallback ICICI_BREEZE_SESSION_TOKEN)
BREEZE_SECRET_ID=    # optional AWS Secrets Manager id/arn -> {"api_key","secret_key","session_token"}
```

## 17. CloudWatch / operations

Structured JSON log events (existing logger → stdout → CloudWatch agent):
`market_data.start` / `.stop` / `.persist` / `.stale` / `.database_error` /
`.subscription_started` / `.subscription_stopped`, `breeze.connected` /
`.disconnected` / `.reconnected` / `.session_invalid`. `FEED_STATUS`
carries `symbols_live`, `option_contracts_subscribed`, `reconnect_count`,
`last_tick_at` for `GET /api/market/health`.

## 18. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `session: SESSION_REQUIRED` | POST today's Breeze token to `/api/market/session` |
| Feed `CONNECTING`, never `RUNNING` | no tick received — check Breeze subscription / market hours / index entitlement (SENSEX needs BSE) |
| `indices` all `no_data` | feed not started (scheduler window / `MARKET_DATA_ENABLED` / session) |
| option chain empty | instrument master not refreshed — check `market_data.instrument_master` log; verify NFO security-master download |
| candles endpoint empty | 1-minute candles are written during market hours only |
| `market_data.database_error` | DB unreachable — candles retry next flush; check `/api/health` |

## 19. How strategies consume market data

Not migrated in this stage. The service is the normalized source; a later
phase wires strategies to:
```
market_data.get_index_quote("NIFTY" | "BANKNIFTY" | "INDIA_VIX" | "SENSEX")
market_data.get_nifty_option_chain(expiry=None, strike_range=None)
market_data.get_option_quote(contract)   /   get_option_candles(contract)
```
Combined premium / VWAP: read ATM CE and PE `ltp` + `vwap` from the chain,
sum for combined premium; the 1-minute `close`/`vwap` candles back a
combined-VWAP series without any Breeze SDK dependency in the strategy.
Existing strategies keep their current behavior until explicitly migrated.
