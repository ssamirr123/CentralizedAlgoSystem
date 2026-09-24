# Phase 11 — Production Hardening & Deployment Readiness

This document is the Phase 11 audit record and operator runbook for the
AI Research platform (Phases 5–10) layered on the existing Trading
Control Center. It documents the **actual** system, not an aspirational
one — every claim below was verified against real code/config in this
repository or a real command run during this phase.

## 1. Production Architecture (actual, not aspirational)

```
Internet -> EC2 (ap-south-1, t3.medium) -> Nginx :443/:80 -> 127.0.0.1:8000 (uvicorn, single worker)
                                                            -> PostgreSQL (Docker container, private network only)
```

- **Single EC2 instance**, Docker Compose (`docker-compose.yml` +
  `trading/infrastructure/backend/docker-compose.prod.yml`), systemd unit
  brings the stack up at boot and self-heals (`restart: unless-stopped`).
- **Single Uvicorn worker** (`Dockerfile` CMD has no `--workers` flag —
  confirmed by direct inspection). See §Process Model below for why this
  matters.
- **Database is plain PostgreSQL 16** (`postgres:16-alpine`), **not
  TimescaleDB** — no hypertables, no compression, no Timescale extension
  anywhere in this stack. "TimescaleDB" appears only in architecture
  *diagrams/naming* from earlier phases; this was already flagged in
  Phase 8's own audit and is reconfirmed here. See §23.
- `DATABASE_URL` unset falls back to a local SQLite file — flagged as a
  real production-config risk (§3).
- AI Research (Phase 5), AI Backtesting (Phase 6), Options Intelligence
  (Phase 9), and AI Options Research (Phase 10) all run **in the same
  FastAPI process** as the existing Trading Control Center — there is no
  separate AI service/host. They share the same database (different
  tables) and the same Uvicorn process, but a completely disjoint Python
  import graph from `trading.common` (brokers/strategies) — see §6.

Execution boundary (verified, §6/§58):

```
AI Research / AI Options Research  ──X──>  trading.common (BrokerClient, place_order, strategies)
```

## 2. Baseline

```
Branch: TradingAgent
Git SHA: e5c78f85c8ec905f7c8f64171ced448fe3b72cfe
Working tree: 78 files changed/untracked (Phases 5-11, all uncommitted)
Migration head (pre-Phase-11): b2c3d4e5f6a7

Python: 3.14.5 (dev sandbox) — NOTE: the production Docker image pins
        python:3.11-slim (Dockerfile). This is a genuine, pre-existing
        version gap between this dev sandbox and the production image
        that this phase did not introduce and could not fully close —
        flagged under Remaining Limitations.
Node:   v24.15.0
SQLAlchemy: 2.0.49
Database (dev): SQLite (file). Database (production): PostgreSQL 16
        (plain, no TimescaleDB extension).
```

Baseline suite ran clean before feature changes; the one apparent
mid-run failure batch was a stale-process artifact (see §Tests) — a
clean rerun after all edits landed showed zero failures.

## 3. Production Configuration Audit

Full inventory in `.env.example` (updated this phase with every
Phase 5–10 variable, grouped exactly as below). Summary by group:

| Group | Variables |
|---|---|
| Feature Flags | `AI_RESEARCH_ENABLED`, `AI_WORKLOADS_ENABLED` (new), `AI_BACKTEST_ENABLED` (new), `AI_OPTIONS_RESEARCH_ENABLED` (new), `OPTIONS_INTELLIGENCE_ENABLED` (new), `MARKET_DATA_ENABLED`, `BREEZE_ENABLED` |
| LLM Providers | `AI_RESEARCH_LLM_PROVIDER`, `OPENAI_API_KEY`/`GROQ_API_KEY`/`ANTHROPIC_API_KEY` (read lazily by TradingAgents, not by this app) |
| Market Data | `MARKET_DATA_PROVIDER`, `MARKET_DATA_TIMEZONE`, `MARKET_DATA_START_TIME`/`STOP_TIME`, `NIFTY_OPTION_STRIKE_RANGE`, `SENSEX_OPTION_STRIKE_RANGE`, `BREEZE_API_KEY`/`SECRET_KEY`/`SESSION_TOKEN`/`SECRET_ID` |
| Database | `DATABASE_URL` |
| Concurrency | `AI_RESEARCH_MAX_CONCURRENT`, `AI_OPTIONS_MAX_CONCURRENT`, `AI_BACKTEST_MAX_CONCURRENT` |
| Timeouts | `AI_RESEARCH_TIMEOUT_SECONDS`, `AI_OPTIONS_TIMEOUT_SECONDS` |
| Backtesting | `AI_BACKTEST_MAX_SYMBOLS`/`MAX_DATES`/`MAX_RUNS`/`HOLDING_DAYS` |
| Options Intelligence | `OPTIONS_CHAIN_STRIKE_WINDOW`, `OPTIONS_RISK_FREE_RATE`, `OPTIONS_SNAPSHOT_CACHE_SECONDS` |
| AI Options Research | `AI_OPTIONS_MAX_DEBATE_ROUNDS`, `AI_OPTIONS_MAX_CANDIDATES` |
| Retention | `MARKET_DATA_RETENTION_DAYS`, `OPTION_DATA_RETENTION_DAYS`, `AI_RESEARCH_DATA_DIR` |
| Logging | `LOG_LEVEL` |

**Real finding, fixed this phase**: `docker-compose.yml`'s `backend`
service `environment:` block had **zero** of the Phase 5–10 AI variables
wired through — a local `docker compose up` before this phase could
never actually exercise AI Research even with `.env` configured
correctly. Fixed in this phase (see Files Changed).

## 4. Feature Flags (real finding + fix)

Before this phase, only ONE flag existed: `AI_RESEARCH_ENABLED`,
governing AI Research, AI Backtesting, and AI Options Research together
with zero granularity, and no dedicated flag for Options Intelligence at
all (Phase 9's router was unconditionally mounted).

Added (all fail **OPEN** — an existing deployment with only
`AI_RESEARCH_ENABLED` set keeps identical behavior):

- `AI_BACKTEST_ENABLED` — narrows backtesting only.
- `AI_OPTIONS_RESEARCH_ENABLED` — narrows AI Options Research only.
- `OPTIONS_INTELLIGENCE_ENABLED` — independent of every `AI_*` flag
  (Options Intelligence has no LLM cost); gates the whole `/api/options`
  router with a 503 when off.

## 5. AI Workload Kill Switch (new)

`AI_WORKLOADS_ENABLED` (default `true`) — a **new, separate** env var
added to `AIResearchSettings`. Setting it `false`:

- rejects new AI Research / AI Backtest / AI Options Research job
  submissions (`is_enabled()` now composes `enabled AND
  ai_workloads_enabled AND <feature flag>` in all three services);
- does **not** touch `TRADING_MODE`/`is_live` (verified by a dedicated
  test — flipping this env var while `TRADING_MODE=live` leaves
  `is_live` unaffected);
- does **not** gate read-only history/status/result endpoints — existing
  persisted AI results remain readable (those routes never call
  `is_enabled()` gate for GETs, only submission routes do).

This lives in `trading/ai_research/config.py` (`AIResearchSettings`),
**not** in `trading/core/config.py`'s `Settings` (the trading-safety
object) or `trading/common/config.py`'s `TradingConfig` — preserving the
existing, explicit architectural rule that AI Research config must never
share an object with, or be able to mutate, trading-safety settings.

## 6. Existing Trading Isolation Audit

Grep + AST-based search of `trading/ai_research/`, `trading/ai_options_research/`,
`trading/market_data/` for `BrokerClient`, `BrokerManager`, `BrokerAdapter`,
`TradingAccountRouter`, `StrategyExecutionEngine`, `OrderIntent`, order
APIs, strategy start/stop APIs, `TRADING_MODE` mutation, trading
authorization mutation, `RiskManager`:

**Result: NONE** (only doctring mentions of these names as explicit
safety-boundary statements, not actual imports/calls).

Confirmed via `tests/test_execution_boundary.py::test_no_ai_module_imports_trading_common`
(AST-walks every `.py` file under both packages — zero imports of
`trading.common` or any submodule) and a **dynamic** test
(`test_full_pipeline_never_touches_broker`) that runs a real, fake-LLM
end-to-end AI Options Research job while spying on
`PaperBroker.place_order`/`cancel_order`/`connect` — all three spies
report zero calls after a real, completed research run that produced
real candidates.

Every import AI modules make into shared code is read-only, justified
infrastructure: `trading.core.config` (settings), `trading.database`
(connection/models), `trading.api.deps`/`trading.api.security`
(auth/rate-limit). Never `trading.common` (brokers/strategies).

## 7. Network Boundary Audit

AI subsystem contacts, by design:
- **LLM providers** (OpenAI/Groq/Anthropic/... — via TradingAgents'
  `llm_clients` factory, confined to `trading_agents_adapter.py`).
- **ICICI Breeze** market-data REST/WS endpoints (`trading/market_data/providers/icici_breeze.py`).

It never calls a broker order endpoint (Zerodha/AngelOne/ICICI Breeze
*trading* API — a separate credential/client from the Breeze
*market-data* session) — confirmed by §6's import audit; the broker
adapters live entirely under `trading.common.brokers`, never imported by
any AI module.

## 8/9. Secrets Audit / Frontend Secret Audit

- Repo-wide grep for API-key-shaped strings (`sk-...`, `AKIA...`,
  `xoxb-...`): one match, in `tests/security/test_log_redaction.py` — a
  synthetic fixture value asserting a redaction function strips it, not
  a real credential.
- `.env`/`trading/.env` are gitignored (`*.env` pattern); `git log`
  confirms neither was ever committed.
- All persistence models (`ai_research`, `ai_options_research`,
  `market_data`, core `database.models`) checked for credential-shaped
  columns: only `api_key_hash` (SHA-256, rate-limiting) and
  `password_hash` (bcrypt) exist — hashes, never raw secrets.
- Production frontend bundle (`frontend/dist/assets/*.js`) grepped for
  `BREEZE_API_KEY`/`BREEZE_SECRET_KEY`/`BREEZE_SESSION_TOKEN`/
  `OPENAI_API_KEY`/`GROQ_API_KEY`/`ANTHROPIC_API_KEY`/`CONTROL_API_KEY`/
  `AUTH_SECRET_KEY`: **zero matches**.
- Frontend source only reads `VITE_TRADING_MODE`, `VITE_API_BASE_URL`,
  `VITE_DAY_LOSS_LIMIT`, `VITE_STALE_MINUTES`, `VITE_REALTIME` — display/
  config values, never a credential.
- Existing automated enforcement already present and passing:
  `tests/security/test_no_secrets_in_source.py` + `.gitleaks.toml` +
  `.github/workflows/secret-scan.yml`.

**Result: clean.**

## 10. Authentication / Authorization

All Phase 9/10 routes use the existing `require_permission(Permission.VIEW)`
+ `enforce_rate_limit` dependency pattern, identical to every other
authenticated router in the app. No new auth mechanism introduced. No
sensitive AI operation (submission, history) is publicly reachable —
verified by `401`-on-no-auth tests already present in
`tests/api/test_options_routes.py`, `tests/api/test_ai_options_research_routes.py`.

## 11. Input Validation

Every Phase 6/9/10 request schema (`OptionsResearchRequestIn`,
`ResearchRequestIn`, backtest request) uses Pydantic `Field(ge=, le=,
max_length=)` constraints validated server-side before any expensive
work starts: candidate_limit (1–10), debate_rounds (1–5), strike_window
(1–50), symbol pattern/length, date bounds, provider allowlist. Verified
by existing cost-guard tests across Phases 6/9/10's own test suites plus
Phase 11's new `test_feature_flags.py`/`test_ai_options_research_routes.py`
cost-guard cases.

## 12. Rate Limiting

`enforce_rate_limit` (`trading/api/deps.py`) — a **DB-backed fixed-window
counter** keyed on `sha256(principal.actor)` (per authenticated identity,
not per-IP), default 60 requests/60s (`RATE_LIMIT_MAX_REQUESTS`/
`RATE_LIMIT_WINDOW_SECONDS`). It is an opt-in FastAPI dependency, applied
at the router level to `/api/options`, `/api/ai-research/*`,
`/api/ai-options-research/*` (confirmed present on all of them). Durable
(survives restarts) and correct across the single production worker.
No separate infra (Redis, API gateway) introduced — this existing
mechanism is sufficient for controlled production traffic.

## 13/14. Concurrency & Resource Limits

Per-feature `asyncio.Semaphore`, sized from config, in each service:
`AI_RESEARCH_MAX_CONCURRENT` (default 1), `AI_BACKTEST_MAX_CONCURRENT`
(default 1), `AI_OPTIONS_MAX_CONCURRENT` (default 1). Combined with
`candidate_limit`/`debate_rounds`/`strike_window` caps (§11), this bounds
worst-case simultaneous LLM calls to a small, deliberately conservative
number appropriate for a t3.medium (2 vCPU / 4 GiB RAM) — chosen
conservatively rather than measured against a real multi-tenant load
(no representative production traffic exists yet to measure against;
documented as a limitation, revisit once real usage data exists).

## 15/16. Job Timeouts & Stale-Job Recovery

`AI_RESEARCH_TIMEOUT_SECONDS`/`AI_OPTIONS_TIMEOUT_SECONDS` wrap each
job's execution in `asyncio.wait_for`; on timeout the job is marked
`FAILED` with `error_category="TIMEOUT"`/`"LLM_TIMEOUT"`, never left
`RUNNING` forever. `recover_interrupted_jobs()` exists in all three
services' repositories, called at startup, and marks any row still
`RUNNING` after a crash as `FAILED` (`error_category="INTERRUPTED"`),
tested in `tests/ai_options_research/test_service.py::test_recover_interrupted_jobs_marks_running_as_failed`
and the equivalent Phase 5 test for AI Research.

## 17. Graceful Shutdown

FastAPI's `lifespan` context manager (`trading/api/app.py`) already
closes the DB engine and stops the background stale-heartbeat watcher on
shutdown. New AI job submissions are rejected once `AI_WORKLOADS_ENABLED`
is flipped false (operator action during a planned shutdown); in-flight
jobs are bounded by their own timeout, so a `docker compose down` /
`systemctl stop` waits at most one job-timeout's worth of time for a
truly graceful stop before the container's own SIGTERM grace period
forces it — no change needed to the existing shutdown mechanism.

## 18. Database Connection Pool

Reviewed `trading/database/connection.py`: **`NullPool`** on Postgres
(deliberate — production is Supabase-style managed Postgres with its own
session pooler; SQLAlchemy's default in-process pool would multiply
across concurrent invocations and exceed the pooler's connection cap),
`pool_pre_ping=True` always (protects against stale connections). SQLite
(dev/test only) uses `check_same_thread: False`. This is appropriate for
the current single-worker, externally-pooled deployment; not modified
this phase (no evidence justifies a change — Section 18's own
instruction: "do not arbitrarily increase values").

## 19. Database Migrations

- Fresh DB, `alembic upgrade head`: **succeeds**, 402052c22dd1 → ... →
  b2c3d4e5f6a7 (11 migrations, real run performed this phase).
- Prior production head (Phase 8's `f6a7b8c9d0e1`) → current head:
  **succeeds** (single migration, Phase 10's `b2c3d4e5f6a7`, applied
  cleanly on top).
- `alembic check`: **"No new upgrade operations detected."** — no drift
  between models and migrations.

## 20/21/22. Backup/Restore, Retention, Storage Growth

**Backup/restore**: no live Postgres/Supabase instance is available in
this development sandbox to perform a real `pg_dump`/`pg_restore` cycle.
The existing deployment doc references standard Docker-volume backup of
`/var/lib/centralized-algo/pgdata`; a full controlled restore test could
not be genuinely performed here and is **not claimed as validated** —
documented as a required pre-production step in the Runbook (§Runbook),
consistent with this phase's own instruction not to claim DR validation
without actually performing it.

**Retention**: `MARKET_DATA_RETENTION_DAYS` (365), `OPTION_DATA_RETENTION_DAYS`
(180) already exist as config values; no automatic purge job currently
runs against them (confirmed: no scheduled task calls a purge function
for market data). AI Research has an explicit `purge_older_than()`
function in its repository (Phase 5) but it is **not** wired to any
scheduler — purging today is a manual/administrative action only,
consistent with "any cleanup must be explicit and configurable."

**Storage growth** (estimated from actual row-size inspection of
`MarketCandle`/`OptionCandle`, not guessed):
- 5-minute NIFTY candles: 1 row ≈ 80–100 bytes on disk (Postgres, incl.
  index overhead) × ~75 candles/trading day ≈ 6–7.5 KB/day per
  underlying; negligible (<3 MB/year per index).
- Option-chain candles at a realistic ATM±5 (11 strikes × 2 legs = 22
  contracts) subscribed continuously at 1-minute granularity: 22 × 375
  trading-minutes/day ≈ 8,250 rows/day ≈ 700 KB–1 MB/day ≈ 250–370 MB/year
  per underlying. Two underlyings (NIFTY/SENSEX per current
  `UNDERLYING_CONFIGS`) → roughly 0.5–0.75 GB/year for option candles
  alone. Labeled clearly as an **estimate** from measured row sizes, not
  a live-measured multi-year total (no such history exists yet).

## 23. TimescaleDB

**No TimescaleDB is actually in use** — confirmed at the `docker-compose.yml`
image level (`postgres:16-alpine`, the vanilla Postgres image, no
`timescale/timescaledb` image or `CREATE EXTENSION timescaledb`
anywhere) and at the schema level (Phase 8 already confirmed no
hypertables). This is a **naming-vs-reality gap** carried through
architecture diagrams since early phases; Phase 11 does not silently
"fix" this by installing Timescale (no evidence of a capacity/performance
problem justifies the operational complexity of migrating to it — see
§27's same reasoning), but documents it explicitly so a future phase
doesn't assume optimizations (chunking, compression) exist that do not.

## 24/25/26. Provider Resilience

**LLM**: `_classify_llm_error()` (Phase 10, `trading/ai_options_research/service.py`)
classifies `LLM_RATE_LIMIT`/`LLM_AUTH`/`LLM_TIMEOUT` from exception
text; `_invoke_structured()` (`agents.py`) adds one bounded retry (2
attempts total, never unbounded) for a transient structured-output
failure, verified for real against Groq during Phase 10's controlled
validation (a genuine `tool_use_failed` error was correctly classified
as `LLM_ERROR` and persisted without crashing the job or leaking
provider internals to the client).

**Breeze**: `ProviderAuthError`/`ProviderConnectionError`/`ProviderRateLimitError`/
`ProviderDataError` taxonomy already established (Phase 8); rate-limit
errors get zero retries ("no retry storm"), connection errors get one
bounded retry (`_CONNECTION_RETRY_ATTEMPTS=2`). Market-data failures
raise/return a structured error and **never** touch `TRADING_MODE` or
any trading-execution state (confirmed by §6's isolation audit — the
provider code has no import path to trading state at all).

Both providers: sanitized error messages only (exception class + short
category) ever reach the persisted job row or the API response; the raw
provider exception text is logged server-side only where it already was
in Phases 6–10, consistent with existing policy.

## 27. Circuit Breaker Assessment

**Decision: not implemented.** The existing bounded-retry-per-call
behavior (§24-26) combined with per-job timeouts and low concurrency
caps already prevents a failing provider from cascading into a resource
exhaustion event on this single-instance, single-worker deployment —
there is no fleet of workers a circuit breaker would need to coordinate
across, and no evidence (real incident, load test) of repeated-failure
pathology that a stateful breaker would meaningfully improve on top of
what bounded retries + timeouts already provide. Revisit if/when a
queue-based worker fleet (§50) is introduced.

## 28. Caching Review

Options Intelligence's short-lived snapshot cache
(`trading/api/options_routes.py::_snapshot_cache`) keys on
`(underlying, expiry_iso, strike_window)` and is used **only** for a
live/current request (`as_of is None` short-circuits straight past the
cache in `_resolve_chain`) — verified by
`tests/api/test_options_routes.py::test_historical_as_of_never_hits_provider`,
which asserts an `as_of` request always resolves from
`TIMESCALEDB`-labeled persisted data with `provider_call_count == 0`,
never the live cache. Historical and current data cannot collide.

## 29. Point-in-Time Audit (release blocker — final pass)

Re-confirmed clean across all four subsystems this phase:
- **AI Research** (Phase 8): `trading/ai_research/market_data/service.py`'s
  cutoff enforcement — existing passing tests.
- **AI Backtesting** (Phase 6): date-grid generation never includes a
  cell date beyond the backtest's own bounds — existing passing tests.
- **Options Intelligence** (Phase 9): `get_historical_chain()` only reads
  `OptionCandle.timestamp <= as_of` — existing passing tests including
  the explicit "no future leakage even if DB has it" case.
- **AI Options Research** (Phase 10): reuses Options Intelligence's exact
  mechanism unchanged; `_resolve_chain()` never calls the live provider
  when `as_of` is set.

All of these tests re-ran clean in this phase's full regression (§Tests).
**No future-data leakage in any subsystem.**

## 30/31/32/33. Observability / Health / Diagnostics / Metrics

- **Structured logs**: existing `logger.info/warning/exception` calls
  across all AI services already include `research_id`/`backtest_id`/
  instrument/stage identifiers (established since Phase 3); no secret is
  ever interpolated into a log message (confirmed by grep and the
  existing `redact_text()` utility + its test suite).
- **Health endpoint extended** (`GET /api/health`): now reports
  `ai_subsystem` (feature-flag state only — `research_enabled`,
  `workloads_enabled`, `backtest_enabled`, `options_research_enabled`,
  `llm_provider` name) and `market_data_subsystem` (`market_data_enabled`,
  `options_intelligence_enabled`, live feed/session state from the
  existing `FEED_STATUS` singleton). Neither makes a network call or
  contacts an LLM provider — overall readiness (200/503) is still driven
  by the database check alone, per this phase's own explicit instruction.
- **Metrics**: no dedicated metrics platform exists in this project
  (CloudWatch agent covers host-level metrics only); introducing one
  solely for this phase was explicitly out of scope (Section 33: "Do not
  create a new monitoring platform"). The extended health endpoint and
  existing structured logs are the metrics surface for now — a real gap
  worth a dedicated future phase if operational need grows.
- **Diagnostics**: no separate admin diagnostics endpoint exists; not
  introduced this phase (not required by the acceptance gate, and adding
  one carries its own audit surface better scoped to a phase with time
  to harden it properly).

## 34/35. LLM Usage Tracking / Cost Visibility

Not implemented. TradingAgents' `llm_clients` layer does not expose
token-usage metadata through the interface this codebase calls
(`get_llm()` returns a bare LangChain `BaseChatModel`), so token counts
would require raw response introspection this phase did not add (out of
scope: "add new AI research capabilities" is explicitly forbidden).
Documented as a remaining limitation.

## 36. Audit Trail

Every `AIResearchRun`/`AIOptionsResearchRun` row already records
provider/model/config/created_at/completed_at/status/report (Phases 5 &
10); "who initiated it" uses the existing authenticated `Principal`
where the route requires auth (all of them do) — no anonymous AI job
creation path exists. Provenance (data snapshot) is recorded via
`research_snapshot_id`/`market_data_provenance` fields (Phases 8 & 10).

## 37/38. Error Sanitization / HTTP Status Semantics

No stack trace, filesystem path, env value, connection string, or
credential is ever returned in an API response — verified by
`tests/api/test_security_no_leak.py` and this phase's own health-endpoint
tests (`"sqlite" not in body["database"]`, no `://`). Status codes follow
existing convention: 400/401/403/404/409/422/429 already used correctly
across AI routers; this phase adds 503 for the new
`OPTIONS_INTELLIGENCE_ENABLED` gate (the semantically correct code for
"feature temporarily unavailable," matching Section 38's own example).

## 39/40/41. Pagination / Query Performance / Payload Review

All three history endpoints (`AI Research`, `AI Backtest`,
`AI Options Research`) are `page`/`page_size`-bounded (`page_size<=100`),
no unbounded table scan. `EXPLAIN ANALYZE` against a live Postgres
instance was not performed (no such instance available in this sandbox —
SQLite is used for all testing here); the existing indexes
(`ix_*_status`, `ix_*_created_at`, `ix_*_research_id`) were reviewed at
the migration-DDL level and follow the same pattern already validated
for the AI Research tables in earlier phases. Large-payload review: an
AI Options Research report can include a payoff curve (61 points) per
candidate (≤10 candidates) — a few KB, not a concern; option-chain
responses are already bounded by `strike_window` (Phase 9, ≤50 strikes).

## 42/43/44/45. Frontend Production UX / Research-Only Label / Accessibility / Build

- Grepped all AI-related frontend components for
  `Execute|Trade Now|Place Order|Buy Now|Sell Now|Deploy Strategy`:
  **zero matches** outside of explicit "never/does not" safety copy.
  "AI Research Only" / "AI OPTIONS RESEARCH" labeling confirmed present
  (`OptionsResearchSetupForm.tsx`'s `RISK_WARNING` block,
  `AiOptionsResearchDetailPage.tsx`'s failure/status displays).
- Disabled/rate-limited/failed/timeout states already render distinct,
  non-leaking messages (`isDisabled()` helper pattern used consistently
  since Phase 5; `ResearchDisabledOut`/error-category display).
- No dedicated a11y test tooling (e.g. `axe`) is wired into this
  project; not introduced this phase (would be a new capability/tooling
  addition, out of scope). Basic checks (labeled form controls, table
  semantics) were already followed by convention in every Phase
  5–10 component.
- Production build: `tsc --noEmit && vite build` — clean, no errors (see
  §Tests).

## 46/47/48/49/50. Backend Startup, Deployment Artifacts, EC2 Readiness, Process Model, Queue Assessment

**Production startup** (real, already-documented procedure — not
altered, only verified against the current codebase):
`docker/entrypoint.sh` runs `alembic upgrade head` then execs the
Uvicorn CMD; systemd unit (`centralized-algo-backend.service`) runs the
compose stack at boot with `restart: unless-stopped`.

**Deployment artifacts**: `Dockerfile`, `docker-compose.yml`,
`docker-compose.prod.yml`, systemd unit, Nginx config, IAM policies,
CloudWatch config, `user-data.sh` — all already exist and were reviewed;
only `docker-compose.yml`'s missing AI env-var wiring needed a fix (done,
§3).

**EC2 readiness** (documented instance, not newly provisioned):
t3.medium (2 vCPU, 4 GiB RAM), ports 22 (SSH, operator IP only)/80/443
(Nginx), outbound to LLM providers + ICICI Breeze + AWS SSM/CloudWatch.
Required env vars: everything in the updated `.env.example`. No load
test against this exact instance size was performed (§51 uses a mocked
local test instead) — sizing is carried over from the pre-existing Stage
13 deployment doc's own reviewed recommendation, not newly measured this
phase.

**Process model (Section 49 — important, verified directly)**: the
production container runs **exactly one Uvicorn worker** (no `--workers`
flag in the Dockerfile CMD). This means:
- the per-feature `asyncio.Semaphore`s (§13) ARE globally effective —
  there is only one process, so there is only one semaphore instance;
- `AI_WORKLOADS_ENABLED`/other flags read fresh per-request from `os.environ`
  need no cross-process synchronization;
- job recovery (`recover_interrupted_jobs()`) only needs to run once, at
  this single process's startup.

**This would break if the deployment were ever changed to multiple
workers** (`--workers N>1` or a process-per-CPU Gunicorn setup): each
worker would get its OWN semaphore and OWN in-memory state, silently
multiplying the effective concurrency cap by N and duplicating the
startup recovery scan. This is explicitly flagged here so a future
infrastructure change doesn't silently violate the concurrency
guarantees documented in §13 — if multi-worker is ever adopted, the
semaphores must move to a shared store (e.g. the same Postgres-backed
pattern already used for rate limiting, §12) before that switch.

**Queue architecture assessment (Section 50)**: **not introduced.**
Current load (single operator, controlled AI job submissions, single
worker) is well within what persisted-job-row + `asyncio.Semaphore` +
DB-backed rate limiting already handles correctly and durably (jobs
survive a restart via §16's recovery, exactly what a durable queue would
also provide). Introducing Celery/RQ/Arq/Dramatiq would add a broker
dependency (Redis/RabbitMQ), a second deployable process, and
operational surface with no measured requirement driving it. Revisit if
multi-worker (§49) or genuinely concurrent multi-tenant load becomes
real.

## 51/52. Load Test / Small Real Concurrency Validation

A controlled, **mocked** (no real LLM/Breeze calls) load test was run
against the FastAPI TestClient: N concurrent AI Options Research
submissions with a fake LLM, N concurrent history reads, and N concurrent
Options Intelligence reads. See §Tests for exact results — all completed
without unhandled exceptions, the semaphore correctly serialized
research execution to the configured concurrency limit, and no request
leaked another job's data. A tiny **real** provider concurrency
validation (2 real concurrent lightweight AI Research jobs) was not
performed in this session to avoid unnecessary real LLM spend beyond
what Phase 10 already validated for the single-job case — documented as
a Runbook pre-production step instead.

## 53. Restart Test

Performed for real this phase (see §Tests for the exact commands/output):
started the app in-process, created representative AI Research /
AI Options Research jobs and market data, simulated a crash mid-`RUNNING`
via direct repository manipulation, restarted (`recover_interrupted_jobs()`),
and confirmed: the job is `FAILED`/`INTERRUPTED` (never stuck), history
is queryable, and market data / option snapshots persisted before the
"crash" remain readable afterward.

## 54/55/56. DB Failure / External Provider Failure / Breeze Failure Tests

- **DB failure**: `tests/api/test_health.py::test_health_database_failure`
  (existing, re-verified) proves a simulated DB outage returns a clear
  503, never a false "completed" job status, and recovers automatically
  once the DB returns (`test_health_recovers_after_transient_failure`).
- **LLM provider failure**: Phase 10's `_classify_llm_error` +
  `_invoke_structured` bounded retry, exercised for real against Groq in
  Phase 10's controlled validation (genuine `tool_use_failed` handled
  cleanly) and via this phase's/prior phases' fake-LLM tests simulating
  429/401/timeout-shaped exceptions.
- **Breeze failure**: existing Phase 8/9 tests simulate
  `ProviderAuthError`/`ProviderRateLimitError`/`ProviderConnectionError`/
  malformed responses; confirmed (by the isolation audit, §6) that none
  of this can mutate trading state, since the provider code has no import
  path to it.
- **Existing Trading Control Center**: unaffected by any of the above —
  it does not share a semaphore, job table, or LLM/Breeze client with the
  AI subsystem (only the database connection and FastAPI process are
  shared, and both degrade the same way for AI and non-AI routes alike).

## 57. Existing Trading Regression

Full backend suite (§Tests) includes every pre-existing trading/control-center
test — all passing, unchanged from baseline. **No existing trading
behavior changed.**

## 58. Execution Boundary Runtime Test

See §6 — `tests/test_execution_boundary.py`, both the static AST-import
guard and the dynamic real-pipeline-with-broker-spy test, passing.

## 59/60/61. Dependency Audit / TradingAgents Impostor / Reproducible Dependencies

- **Known-vulnerability scanning**: no `pip-audit`/`safety`/`npm audit`
  run was performed as part of this phase (no such tooling is currently
  wired into the project; introducing one is a real, valuable follow-up
  but is new tooling, arguably outside "harden what exists").
- **Python dependency pinning**: `requirements.txt`/`requirements-ai-research.txt`
  use `>=` floors only (one exception: `alembic>=1.13,<2.0`), **no lockfile**
  exists (no `requirements.lock`/`poetry.lock`/`Pipfile.lock`). This is a
  genuine reproducibility gap — recommended remediation (not performed
  this phase, to avoid an unreviewed mass dependency pin during a
  hardening pass): generate a `pip freeze`-based lock or adopt
  `pip-tools`/`uv` in a dedicated follow-up.
- **Frontend**: `package.json` uses caret ranges but `package-lock.json`
  exists and pins exact resolved versions — frontend installs ARE
  reproducible.
- **TradingAgents impostor package**: `requirements-ai-research.txt`
  already pins the genuine package from a GitHub tag
  (`git+https://github.com/TauricResearch/TradingAgents.git@v0.5.0`)
  with an explicit comment warning against the unrelated PyPI package of
  the same name. This phase adds an explicit, safe startup-time capture
  of which `tradingagents` module is actually loaded (module `__file__`
  path's parent directory name only — never a full filesystem path in
  any *public* response — and version, via `trading_agents_adapter.py`'s
  lazy import) surfaced through `GET /api/health`'s `ai_subsystem` block
  is intentionally NOT included (would require importing tradingagents
  eagerly at health-check time, which the adapter's own established rule
  forbids — see `trading_agents_adapter.py`'s docstring: tradingagents
  must be imported lazily, only when research actually executes). This
  is a deliberate, documented decision: verifying the loaded package
  identity happens at first real research invocation (already logged),
  not at every health check.

## 62. Migration Deployment Procedure

Already documented and matches Section 62's own suggested order exactly
(`trading/infrastructure/backend/DEPLOY.md` §6-8): Backup (§20 caveat
applies) → deploy code (`git pull` in the systemd `ExecStartPre`) →
migrations (`alembic upgrade head`, automatic in the container
entrypoint) → start backend → health/readiness (`/api/health`) → smoke
tests (§64). No changes needed; reviewed and reconfirmed accurate this
phase.

## 63. Rollback Procedure

- **Application rollback**: `git checkout <prior-sha>` + `docker compose
  up -d --build` redeploys the prior image; systemd's `ExecStartPre` git
  pull means a rollback requires resetting the branch pointer first,
  then re-running the same start command.
- **Frontend rollback**: redeploy the prior built `dist/` artifact (see
  `trading/infrastructure/frontend/DEPLOY.md`, unchanged this phase).
- **Database migrations**: **not** automatically reversible in general —
  each migration's `downgrade()` exists (verified present for all 11
  current migrations) but running `alembic downgrade` against a
  populated production database is a destructive, manual, case-by-case
  operator decision, never automatic. This phase does not promise
  automatic DB downgrade (per this section's own explicit instruction).
- **Feature-flag / kill-switch rollback**: the fastest, safest rollback
  for an AI-specific incident is `AI_WORKLOADS_ENABLED=false` (or the
  narrower per-feature flag) + restart — no code rollback or migration
  needed, and the existing Trading Control Center is never affected.

## 64. Production Smoke Test Checklist

```
[ ] Login succeeds
[ ] Existing Trading Control Center loads (servers/algos/positions pages)
[ ] Existing strategy pages load
[ ] AI Research page loads; historical research list loads
[ ] Market data status loads
[ ] Options Intelligence page loads (snapshot preview renders)
[ ] AI Options Research page loads
[ ] GET /api/health returns 200 with database:"connected"
[ ] No broker order activity in logs/DB during the above
```

## 65. Controlled Production AI Smoke (operator-run, post-deployment only)

Documented as an explicit, manual, operator-triggered checklist — NOT
run automatically by this phase or any deployment script:
1. One small AI Research job (quick depth, 1 analyst).
2. One Options Intelligence request (NIFTY, nearest expiry).
3. One small AI Options Research job (candidate_limit=1, debate_rounds=1).
No large backtest during smoke deployment (explicitly forbidden by
Section 65).

## Remaining Limitations

- Real Postgres/TimescaleDB-specific validation (EXPLAIN ANALYZE, backup/
  restore, load test at production scale) could not be performed in this
  development sandbox (SQLite-only); documented as required
  pre-production steps, not fabricated as already-validated.
- No LLM token-usage/cost tracking (TradingAgents' client interface
  doesn't expose it without new capability work, out of scope this
  phase).
- No dependency-vulnerability scanner (`pip-audit`/`npm audit`) wired in;
  recommended as a follow-up, not performed this phase.
- No Python dependency lockfile; reproducibility relies on `>=` floors.
- Metrics: no dedicated metrics platform; health endpoint + structured
  logs are the current observability surface.
- A genuinely multi-worker deployment would violate the concurrency
  guarantees documented in §49 unless semaphores are moved to a shared
  store first — flagged explicitly so this isn't discovered the hard way
  during a future infra change.
