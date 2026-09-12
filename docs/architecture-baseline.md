# Architecture Baseline — Broker-Agnostic Trading Control Center

Read-only audit. No application code was modified to produce this document.
Snapshot as of branch `feature/broker-agnostic-execution-framework`,
2026-09-11.

---

## 1. Current Project Structure

```
CentralizedAlgoSystem/
├── trading/
│   ├── algos/                          # Live strategies (one dir per algo)
│   │   ├── DoubleStraddelAlgo/         # PRIMARY strategy for this audit
│   │   ├── CombinedVwapNifty/
│   │   └── Vwap_Algo_Nifty_hedge/
│   ├── common/                         # Shared, broker-agnostic platform code
│   │   ├── broker.py                   # BrokerClient ABC + generic models/exceptions
│   │   ├── brokers/                    # Broker adapters (angelone real; others stubs)
│   │   ├── execution.py                # StrategyExecutionEngine
│   │   ├── order_intent.py             # OrderIntent
│   │   ├── risk_manager.py             # RiskManager (generic, skeleton)
│   │   ├── strategy_assignment.py      # StrategyAssignment (strategy_id -> account_id)
│   │   ├── trading_account.py          # TradingAccount
│   │   ├── broker_manager.py           # BrokerManager
│   │   ├── config.py                   # TradingConfig / BrokerCredentials
│   │   ├── heartbeat.py, reporting.py, logger.py, utils.py
│   ├── tools/
│   │   └── angelone_readonly_validation.py   # Manual, read-only Angel validation CLI
│   ├── market_data/                    # Separate read-only market-data engine (ICICI Breeze)
│   ├── core/                           # Control-center Settings (FastAPI backend)
│   ├── database/                       # SQLAlchemy models/connection (control-center DB)
│   ├── api/                            # FastAPI control-center (auth, routes, realtime)
│   ├── agent/                          # trading_agent.py — on-box process lifecycle (SSM target)
│   └── infrastructure/                 # Deployment: backend/frontend/lambda/scheduler/strategy
├── frontend/                           # React control-center UI
├── tests/
│   ├── common/, algos/, tools/, api/, core/, database/, infrastructure/, market_data/, security/
├── docs/                               # MARKET_DATA.md, SECRETS.md, (this file)
└── pyproject.toml, requirements*.txt, docker-compose.yml, Dockerfile, alembic/
```

Each algo folder under `trading/algos/*` is a **self-contained, independent process** — no cross-imports between algos. All three currently duplicate the same structural pattern (own `config.py`, `connectapi.py`, `websocket_feed.py`, `token_file.py`, order-placement module, `monitor.py`).

---

## 2. Existing Strategy Entry Points

| Algo | Entry point | Launch mechanism |
|---|---|---|
| DoubleStraddelAlgo | `trading/algos/DoubleStraddelAlgo/main.py` | `python main.py`, launched via `trading/agent/trading_agent.py`'s `START_ALGO` (SSM-invoked subprocess) |
| CombinedVwapNifty | `trading/algos/CombinedVwapNifty/main.py` | same mechanism |
| Vwap_Algo_Nifty_hedge | `trading/algos/Vwap_Algo_Nifty_hedge/main.py` | same mechanism |

All three `main.py` files follow the same sequence: `log_setup.init()` → `write_pid_file(<algo_name>)` (control-framework liveness contract) → `monitor.start()` (heartbeat) → `token_file.download_token()` (scrip master) → broker login (`connectapi.makeconnection()`, blocking retry loop) → start WebSocket feed thread → run the strategy's scheduler loop (blocks until EOD or emergency square-off).

None of the three wire in `trading/common/utils.py`'s `GracefulShutdown`/stop-flag contract — `STOP_ALGO` always falls through to a hard OS-level kill after a grace timeout.

---

## 3. Existing Broker/API Integrations

**All three live algos are hardcoded to Angel One SmartAPI directly** — none use the `trading/common/broker.py` abstraction:

- SDK: `SmartApi` (`smartapi-python` + `pyotp`), classes `SmartConnect` and `SmartWebSocketV2`.
- Each algo's own `connectapi.py` performs TOTP+MPIN login; `websocket_feed.py`/`Websocket.py` wraps `SmartWebSocketV2`.
- Session state lives as a **bare module-level global** (`config.objconn`, `config.sws`) read directly from `websocket_feed.py`, the order-placement module, `monitor.py`, and (in DoubleStraddelAlgo) `strategy/hedge.py`.

Separately, **`trading/common/broker.py` + `trading/common/brokers/*`** already exists as the target abstraction (built in Phases 1–2 of this migration):
- `BrokerClient` ABC: `connect/disconnect/is_connected/get_quote/place_order/cancel_order/get_positions`, plus optional duck-typed hooks (`get_order`, `modify_order`) and adapter-specific extras (`get_account_info`, `get_funds`, `resolve_instrument`, `get_order_book`, `get_open_orders`).
- `trading/common/brokers/paper_broker.py` — fully implemented, real (simulated, no network).
- `trading/common/brokers/angelone.py` — **fully implemented, real**, including a `read_only` safety gate (`ANGEL_READ_ONLY` / explicit constructor arg) checked before every mutating call, independent of `TRADING_MODE`. Validated against a live Angel One session (read-only) — see §17.
- `trading/common/brokers/zerodha_kite.py`, `icici_breeze.py` — stubs (`NotImplementedError` on every real method).
- `trading/tools/angelone_readonly_validation.py` — manual CLI proving the real adapter's read operations work end-to-end.

**`trading/algos/DoubleStraddelAlgo/broker/execution_bridge.py`** additively mirrors every real order `broker/orders.py` places into this new stack in **shadow mode only** (hard-coded empty credentials, real order path untouched) — see §17.

`trading/market_data/` is a **third, unrelated** integration: a read-only ICICI Breeze quote/candle/option-chain feed for the control-center dashboard, with its own provider abstraction (`MarketDataProvider`). It has no order-execution capability and is not used by any algo's own strategy loop.

---

## 4. All Order-Placement Code

| Algo | File | Functions |
|---|---|---|
| DoubleStraddelAlgo | `broker/orders.py` | `place_limit`, `place_market`, `modify_limit`, `cancel`, `cancel_all_pending`, `cancel_pending_for_tokens` — all via `config.objconn.placeOrder/modifyOrder/cancelOrder`. Additive shadow-mirror calls into `broker/execution_bridge.py` (Phase 3) added alongside every real call — the real call itself is byte-identical to before. |
| CombinedVwapNifty | `rest_func.py` | `place_market_order`, `place_limit_order`, `modify_limit_order`, `cancel_order`, `execute_limit_order` (reprice/chase loop) |
| Vwap_Algo_Nifty_hedge | `rest_func.py` | `place_market_order`, `place_stoploss_order`, `modify_stoploss_order` (dead code — no caller) — **no cancel-order function exists in this algo at all** |

Every one of these builds an Angel-specific `orderparams` dict (`variety`, `tradingsymbol`, `symboltoken`, `transactiontype`, `exchange`, `ordertype`, `producttype`, `duration`, `squareoff`, `stoploss`, `quantity`) and calls `config.objconn.placeOrder(...)` directly. Retry/rate-limit handling is reimplemented independently in each algo (near-identical `_retry`/backoff pattern, Angel-specific error-string matching for `'exceeding access rate'`).

The new, broker-agnostic equivalent — `trading/common/execution.py`'s `StrategyExecutionEngine` — generalizes this exact retry/reprice/pending-order logic once, against `BrokerClient` only. It is wired into DoubleStraddelAlgo's shadow path only (§17), not into any live order path.

---

## 5. All Market-Data Code

Each algo has its own tick pipeline, entirely Angel-specific:
- `Websocket.py`/`websocket_feed.py`: `SmartWebSocketV2`, tick payload keys `token`/`last_traded_price` (paisa, `/100`), numeric `exchangeType` codes (1=NSE, 2=NFO).
- `make_data.py`: rolls raw ticks into per-minute/2-minute OHLC candles in a shared global dict (`config.tlv_data` → `config.ohlc_data`).
- REST fallback for LTP when the WS cache is stale, via `config.objconn.ltpData(...)`.

`trading/market_data/` (separate, control-center-facing): `providers/icici_breeze.py` (real), `service.py` (feed lifecycle: Breeze tick → live cache → 1-min aggregator → Postgres), `aggregator.py`, `cache.py`, `instruments.py`, `option_chain.py`, `straddle_pulse.py`. Config default provider is `icici_breeze` (only one registered).

---

## 6. Authentication / Session Handling

- Each algo: `connectapi.py`'s `makeconnection()` — TOTP (`pyotp.TOTP(secret).now()`) + client ID + MPIN → `SmartConnect(api_key=...).generateSession(...)`, 3-attempt retry, returns `None` on total failure. Credentials read from env vars with a `trading/.env` file fallback (`os.environ.setdefault`, each algo's `config.py` re-implements this identically).
- No re-authentication/session-refresh logic anywhere if a session expires mid-day.
- **New, broker-agnostic path**: `trading/common/brokers/angelone.py`'s `connect()` — same TOTP+MPIN flow, but converts all failures into the generic `BrokerAuthenticationError`/`BrokerConfigError`/`BrokerConnectionError` vocabulary, and is gated by a `read_only` flag checked before any mutating call. `trading/common/config.py`'s `BrokerCredentials` reads `ANGELONE_API_KEY`/`ANGELONE_CLIENT_ID`/`ANGELONE_PASSWORD` (alias: `ANGELONE_MPIN`)/`ANGELONE_TOTP_SECRET`.

---

## 7. Position Handling

- DoubleStraddelAlgo: strategy logic reasons about its **own local shadow state** (`state.json` via `state/store.py`), not the broker's position book. Broker positions (`config.objconn.position()`) are read only for MTM/kill-switch/monitoring (`risk/guard.py`, `monitor.py`), on a 120s reconciliation interval.
- CombinedVwapNifty: purely self-tracked in-memory globals (`config.in_position`, `config.entry_price`, ...) — **no broker position reconciliation at all**, and no crash-recovery state file (a real hazard: a crash mid-day loses all position knowledge even if the broker still holds it).
- Vwap_Algo_Nifty_hedge: order-status-based only (`config.orderbook` polling), no qty/holdings-based reconciliation.
- New path: `BrokerClient.get_positions()` returns generic `Position` objects; `AngelOneBroker.get_positions()` normalizes Angel's raw field names (`realised`, `unrealised`, `netqty`, `avgnetprice`, `tradingsymbol`) into it. Not used by any live algo yet.

---

## 8. P&L Calculation

Computed independently, multiple times, in each algo — a documented duplication smell:
- DoubleStraddelAlgo: `strategy/straddle.py::session_pnl()`, `report/csv_report.py`, `monitor.py` (`_compute_pnl_mtm_live`/`_sim`), `risk/guard.py::day_mtm()` — 4 separate implementations of essentially the same entry/exit-price arithmetic.
- CombinedVwapNifty: `manager.exit_leg()` (realized) + `monitor._compute_pnl_mtm()` (a second, inconsistent implementation — `cum_loss` only tracks losses, no net realized variable).
- Vwap_Algo_Nifty_hedge: `monitor.py` only, purely broker-derived.

No shared, generic P&L module exists yet in `trading/common/`.

---

## 9. Instrument/Token Handling

**This is the hardest, still-unsolved cross-broker boundary.**

- Each algo's `token_file.py` downloads Angel's public NFO/OPTIDX scrip master (`https://margincalculator.angelbroking.com/...`) and resolves a strike suffix (e.g. `"23950CE"`) to `(tradingsymbol, symboltoken)`, e.g. `("NIFTY15SEP2623950CE", "47317")`. Strategy code stores and threads the raw Angel `symboltoken` directly through order calls (`leg['tok']` in `state.json`).
- `strategy/expiry.py` (DoubleStraddelAlgo) parses the Angel tradingsymbol string **positionally** (`symbol[len(name):len(name)+7]` for expiry, etc.) to derive expiry-day/hedge-strike logic — a second, independent place that assumes Angel's exact symbol format.
- New path: `AngelOneBroker._resolve_instrument()`/public `resolve_instrument()` does its **own, independent** resolution (same public scrip master, injectable resolver for tests) — `BrokerClient.place_order(symbol: str, ...)` accepts only a plain string, no token concept at the interface level. **Confirmed via live validation (§17)**: for the same tradingsymbol, `token_file.py`'s resolver and `AngelOneBroker.resolve_instrument()` returned the identical token (47317) — but this is two independent lookups against the same data, not a shared implementation. No cross-broker instrument-identity abstraction exists.

---

## 10. Configuration / Environment Variables

- **Per-algo** (`config.py`, one copy per algo, near-identical): `ANGELONE_API_KEY/CLIENT_ID/MPIN|PASSWORD/TOTP_SECRET`, `BOT_DRY_RUN`, instrument constants (`INDEX_TOKEN`, `OPT_EXCH`, `LOT_QTY`), timing windows, SL/target points, order-retry tuning, `DAILY_MAX_LOSS`, Telegram settings, `STRATEGY_NAME`/`SERVER_NAME`/`API_BASE_URL` (the latter three are **stale/dead** in each algo's `config.py` — `monitor.py` deliberately ignores them in favor of the env vars `trading_agent.py`'s `START_ALGO` actually injects).
- **Shared** (`trading/common/config.py`): `TRADING_MODE` (paper|live, the single safety gate), `BROKER` (paper|zerodha|angelone|icici_breeze), `ANGEL_READ_ONLY` (new, Phase 4/5 — independent, higher-priority safety gate), `CONTROL_API_KEY`, `HEARTBEAT_INTERVAL_SECONDS`, `BROKER_RECONNECT_MAX_ATTEMPTS/BACKOFF_SECONDS`.
- **Credential fallback file**: `trading/.env` (git-ignored) — read by each algo's own `config.py` via manual line-parsing (`os.environ.setdefault`), and also by `trading/tools/angelone_readonly_validation.py`'s `_load_dotenv_if_present()` (mirrors the same convention for the shared tooling).
- **Control-center backend** (`trading/core/config.py`): superset `Settings` dataclass — DB URL, auth secrets, AWS/Lambda, market-data provider selection, etc. Explicitly documented to share `broker_name`/`trading_mode`/credentials field names with `trading/common/config.py`.

---

## 11. Existing Risk Controls

- DoubleStraddelAlgo: `risk/guard.py` — background daemon recomputing day MTM from the broker position book every `RECONCILE_INTERVAL` (120s), tripping a `kill_switch` + emergency square-off (market orders, cancel-all) if MTM ≤ `-DAILY_MAX_LOSS`.
- CombinedVwapNifty: `manager.check_combined_risk()` — a 3-tier combined CE+PE loss ladder (`RISK_LOSS_LEVELS`), exiting the bigger-losing leg first, both legs + `day_stopped=True` on the final tier.
- Vwap_Algo_Nifty_hedge: **no kill-switch/max-loss guard exists in this algo at all.**
- New path: `trading/common/risk_manager.py`'s `RiskManager` is a **generic, structural-only** skeleton (assignment exists/enabled, quantity>0, valid side/type, limit price present) — it does not implement any of the strategy-specific risk logic above, and only gates the Phase-3 shadow path, never the real one.

---

## 12. Existing Logging

- Each algo has its own `log_setup.py` (near-identical, `_Tee`-based stdout/stderr mirroring to `logs/<date>/app.log`, plus optional Telegram forwarding) — a copy-pasted pattern, not shared.
- Shared control-center logging: `trading/common/logger.py` (`configure_logging()`, structured JSON on stdout) — used by the FastAPI backend, not by the algos.
- New Phase 3/4/5 code (`execution_bridge.py`, `angelone_readonly_validation.py`, `angelone.py`) uses Python's standard `logging` module, with explicit secret-redaction guards (`_assert_no_secrets`/`_sanitize_error`) verified by tests.

---

## 13. Existing Database / Storage

- **No live algo uses a real database.** DoubleStraddelAlgo has `db/schema.sql` (sessions/trades/orders_audit tables) that is **designed but never wired up** — dead code, no `sqlite3`/`psycopg` connection anywhere references it.
- DoubleStraddelAlgo: `state/store.py` → `state.json`, atomic write, single-trading-day scoped, crash-recovery resume.
- CombinedVwapNifty / Vwap_Algo_Nifty_hedge: **no state persistence at all** — a crash loses all in-memory position/loss tracking.
- Each algo caches its own `nifty_token.csv` (scrip master subset) via `token_file.py`, read with `pandas.read_csv` on every lookup (no in-memory cache), path relative to CWD.
- Control-center backend: `trading/database/` — canonical SQLAlchemy models (`servers`, `algos`, `algo_runs`, `heartbeats`, `logs`, `positions`, `trades`, `daily_pnl`, `commands`, `rate_limit_windows`), Postgres via `DATABASE_URL` (SQLite fallback), Alembic migrations. Unrelated to any algo's own trading state.

---

## 14. Existing WebSocket Implementation

Per-algo, entirely Angel-specific (`SmartWebSocketV2`), see §5. `BrokerClient` (the new abstraction) declares **no streaming/subscribe methods at all** — this is an explicitly documented gap (Phase 2's `angelone.py` module docstring): a proper streaming abstraction doesn't exist yet, and none of the Phase 1–5 work touches it. Each algo's WebSocket feed remains completely untouched by the migration to date.

---

## 15. Existing Scheduled Jobs / Threads / Processes

- **In-process daemon threads** (per algo): WebSocket reconnect loop, risk-guard loop, order-book/position reconciliation pollers, pending-order management threads, heartbeat refresh loop (`monitor._refresh_loop`, every 20s).
- **systemd** (`trading/infrastructure/strategy/systemd/`): `centralized-algo-strategy@.service` (templated per-algo unit) + `centralized-algo-strategy-watchdog@.service`/`.timer` (crash-recovery watchdog).
- **AWS EventBridge Scheduler** (`trading/infrastructure/scheduler/`): JSON target payloads for scheduled start/stop of EC2 instances and algos (`target-start-algos.json`, `target-stop-all-ec2.json`, etc.) — broker-agnostic, operates on algo names/instance IDs only.
- **`trading/agent/trading_agent.py`**: the on-box SSM-invoked CLI (`START_ALGO`/`STOP_ALGO`/`RESTART_ALGO`/`STATUS`/`UPDATE`/`LOGS`) — broker-agnostic, PID-file-based liveness, git-pull self-update with conflict recovery. Zero references to any broker.

---

## 16. External Dependencies

**Root (`pyproject.toml`)**: `boto3`, `fastapi`, `httpx`, `pandas`, `psycopg2-binary`, `pydantic`, `sqlalchemy`, `uvicorn`, `requests`, `alembic`, `bcrypt`, `PyJWT`, `breeze-connect` (ICICI market-data only).

**Per-algo `requirements.txt`** (each near-identical): `smartapi-python`, `pyotp`, `pandas`, `numpy`, `requests`. `Vwap_Algo_Nifty_hedge` additionally pins `pandas==2.3.3` and adds `pandas_ta` (requires Python ≥3.12).

No `dhanhq` or Shoonya/Finvasia SDK exists anywhere in the repo — those two target brokers have zero footprint (no adapter, no credential fields, no dependency).

---

## 17. Current Live / Paper / Shadow Modes

| Mode | Where | Behavior |
|---|---|---|
| **Live** (per-algo) | `config.DRY_RUN=False` (or unset in some algos) | Real orders via `config.objconn.placeOrder(...)`. This is the only mode any live algo actually trades in. |
| **Paper/dry-run** (per-algo) | `config.DRY_RUN=True` | Each algo's own order functions short-circuit to a synthetic `DRYRUN-<ms>` id, never touching the broker. Independent per-algo implementation, not shared. |
| **Paper** (new stack) | `BROKER=paper` | `trading/common/brokers/paper_broker.py` — fully implemented, in-memory, instant fills. |
| **Read-only** (new stack) | `ANGEL_READ_ONLY=true` or explicit `read_only=True` | `AngelOneBroker` establishes a **real** authenticated session but `place_order`/`modify_order`/`cancel_order` raise `ReadOnlyModeError` before any SmartAPI mutation call, checked before `TRADING_MODE`. **Validated live** via `trading/tools/angelone_readonly_validation.py` — real authentication, instrument resolution (matching `token_file.py`), quote/LTP, funds, positions, order book, and open orders all passed against the real account; `placeOrder`/`modifyOrder`/`cancelOrder` were never called. |
| **Shadow** (DoubleStraddelAlgo only) | `broker/execution_bridge.py` | Additive mirror of every real order into `OrderIntent → RiskManager → StrategyExecutionEngine → StrategyAssignment → TradingAccount → BrokerManager → AngelOneBroker`. The shadow account's credentials are **hard-coded to four empty strings**, independent of the real env/`TRADING_MODE`, guaranteeing `connect()` fails before any network call — this is deliberately more conservative than "connected shadow" (a real session isn't held by the shadow bridge itself; that's the explicitly-not-yet-approved next step). Runs on a background thread; any failure is swallowed and never affects the real order. |

CombinedVwapNifty and Vwap_Algo_Nifty_hedge have **no shadow integration** — only their own per-algo DRY_RUN flag.

---

## 18. Potential Dangerous Code Paths

1. **No graceful shutdown in any live algo** — `STOP_ALGO` always hard-kills after a grace timeout; no algo cancels pending orders or logs out cleanly on a controlled stop.
2. **No crash-recovery state in CombinedVwapNifty/Vwap_Algo_Nifty_hedge** — a crash mid-day silently loses track of real, possibly-open broker positions.
3. **Rate-limit/error classification is Angel-specific string-matching** (`'exceeding access rate'`) baked into each algo's generic-looking retry helper — silently stops working against any other broker (fixed in the new `AngelOneBroker` via `BrokerRateLimitError`/`BrokerAuthenticationError`, not yet back-ported to the live algos).
4. **`db/schema.sql` is dead code** — a trap for anyone assuming it's load-bearing.
5. **Vwap_Algo_Nifty_hedge has no cancel-order function at all** — pending orders can only be left to sit or filled, never explicitly cancelled by that algo's own code.
6. **P&L is computed 2–4 different, independently-maintained ways per algo** — a bug fix in one implementation doesn't propagate to the others.
7. **`ANGELONE_MPIN`/`ANGELONE_PASSWORD` alias inconsistency** (found and fixed during Phase 5A validation): the shared `trading/common/config.py` only recognized `ANGELONE_PASSWORD` until this audit's preceding session added `ANGELONE_MPIN` as an alias — a real interoperability gap between the per-algo and shared credential loaders that could have silently blocked the shared tooling from ever picking up validly-configured credentials.
8. **Importing any live algo's bare `config` module in a process where a real `trading/.env` exists will populate real credentials into that process's environment** (via `os.environ.setdefault`) — confirmed to actually happen during this migration's own test suite once a real `trading/.env` was added; test-side guards now exist (dummy env pre-seeding, `conftest.py` scrub list) but the underlying per-algo `config.py` behavior itself is unchanged and this risk applies to **any** future code that imports one of these `config.py` modules in-process.

---

## Dependency Diagram — Strategy → Broker API (current state)

```text
┌─────────────────────────┐   ┌─────────────────────────┐   ┌─────────────────────────┐
│   DoubleStraddelAlgo     │   │   CombinedVwapNifty      │   │  Vwap_Algo_Nifty_hedge   │
│  strategy/straddle.py    │   │  manager.py              │   │  manager.py              │
│  strategy/hedge.py       │   │                          │   │                          │
└────────────┬─────────────┘   └────────────┬─────────────┘   └────────────┬─────────────┘
             │ orders.place_limit(sym,tok,..)│ rest_func.place_*(...)      │ rest_func.place_*(...)
             ▼                               ▼                             ▼
┌─────────────────────────┐   ┌─────────────────────────┐   ┌─────────────────────────┐
│   broker/orders.py       │   │   rest_func.py           │   │   rest_func.py           │
│ (+ shadow mirror, Ph.3)  │   │                          │   │                          │
└────────────┬─────────────┘   └────────────┬─────────────┘   └────────────┬─────────────┘
             │  config.objconn.placeOrder(...)         (same pattern, all three)
             ▼                               ▼                             ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                          config.objconn  (SmartApi.SmartConnect)                     │
│                                Angel One SmartAPI                                    │
└───────────────────────────────────────────────────────────────────────────────────────┘

  ---- This is the ENTIRE live/real order path today: 3 lines, 1 broker, 0 abstraction. ----


  Built, tested, validated live (read-only) — NOT wired into any live order path yet:

  OrderIntent → RiskManager → StrategyExecutionEngine → StrategyAssignment
       → TradingAccount → BrokerManager → BrokerClient → AngelOneBroker → Angel One
                                                                (real session validated,
                                                                 mutation calls blocked)

  DoubleStraddelAlgo's real order path ADDITIONALLY, silently, mirrors into the above
  stack in shadow mode only (broker/execution_bridge.py) — hard-coded non-connectable
  credentials, real order path unaffected either way.
```

**Where broker-specific logic currently exists** (exhaustive):
- `trading/algos/*/connectapi.py`, `Websocket.py`/`websocket_feed.py`, `token_file.py`, `broker/orders.py` or `rest_func.py` (all three algos) — direct `SmartApi` imports and Angel-specific request/response shapes.
- `trading/common/brokers/angelone.py` — the one place this is *intended* to live going forward.
- `trading/market_data/providers/icici_breeze.py` — Breeze-specific, market-data only, unrelated to order execution.

---

## BASELINE STATUS: **PASS**

The repository is in a coherent, understood state suitable to continue the broker-agnostic migration from:

- The full live order-placement/market-data/auth/position/P&L/risk surface for all three algos is mapped, with exact file:function locations.
- The target abstraction (`BrokerClient` → `AngelOneBroker`) already exists, is fully implemented, is unit-tested (159 tests), and has been **validated against a real Angel One account in read-only mode** — proving the adapter genuinely works, not just against mocks.
- The one connected integration point (DoubleStraddelAlgo's shadow mirror) is additive-only and does not touch the live order path in any way; this was independently re-confirmed by inspection.
- No real order was placed, modified, or cancelled by anything reviewed in this audit.
- No application/trading logic was modified to produce this document — the only repository state change is the new `docs/architecture-baseline.md` file itself.

This is a **PASS**, not a "no work remains" signal: item 18 above lists concrete, unresolved risks (no graceful shutdown, no crash recovery in two algos, duplicated P&L logic, Angel-specific error handling not yet generalized into the live algos) that any further migration phase should account for, and the instrument-identity and streaming abstractions remain genuinely unsolved cross-broker problems, not oversights.
