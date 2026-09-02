# Stage 19 — Market Data Engine · deployment runbook (DO NOT auto-deploy)

Paper mode only. This adds a read-only market-data engine; it introduces
**no** order path and does not touch Stage 18 auth / RBAC / TRADING_MODE.

Full design: `docs/MARKET_DATA.md`.

## Deployment checklist

1. **Database migration**
   ```
   alembic upgrade head          # applies 323f5b753c2c (market_candles, option_contracts, option_candles)
   alembic check                 # -> "No new upgrade operations detected."
   ```
   Existing tables and data are untouched.

2. **Dependency**
   `breeze-connect` is in `requirements.txt`. Rebuild the backend image
   (`docker compose ... up -d --build`). It is lazy-imported, so a build
   that omits it still starts — but the feed then reports
   `feed: NOT_CONFIGURED / SESSION_REQUIRED` when enabled.
   Run `pip check` in the image and confirm no conflict with
   `websocket-client` / `python-socketio`.

3. **Environment** — append to `/etc/centralized-algo/backend.env`
   (never commit):
   ```
   MARKET_DATA_ENABLED=true
   MARKET_DATA_TIMEZONE=Asia/Kolkata
   MARKET_DATA_START_TIME=09:10
   MARKET_DATA_STOP_TIME=15:45
   NIFTY_OPTION_STRIKE_RANGE=10
   BREEZE_API_KEY=...
   BREEZE_SECRET_KEY=...
   # BREEZE_SESSION_TOKEN=...        # optional bootstrap; normally provisioned via the API
   # BREEZE_SECRET_ID=arn:aws:secretsmanager:ap-south-1:...:secret:breeze
   ```
   Leave `MARKET_DATA_ENABLED` unset for a first deploy that only ships
   the code + migration, then flip it on.

4. **Breeze credentials / daily session**
   - Store `api_key` / `secret_key` in `backend.env` **or** an AWS Secrets
     Manager secret `{"api_key","secret_key","session_token"}` referenced
     by `BREEZE_SECRET_ID` (backend instance role needs
     `secretsmanager:GetSecretValue` on that ARN).
   - **Every trading morning** an admin generates today's session token
     via the Breeze login page and:
     ```
     POST /api/market/session  { "session_token": "<token>" }
     ```
     (or updates the Secrets Manager secret + restarts). The token is
     never returned or logged.

5. **Service startup** — automatic. `create_app()` lifespan starts the
   `MarketDataScheduler` when `MARKET_DATA_ENABLED=true`; it drives the
   09:10 / 15:45 IST startup/stop of `MarketDataService`.

6. **Scheduler startup** — verify from logs after 09:10 IST:
   `market_data.scheduler start window=09:10-15:45 Asia/Kolkata`,
   then `market_data.start done symbols_live=…`.

7. **CloudWatch** — the existing agent ships stdout JSON. New events:
   `market_data.*`, `breeze.*`. No secrets are logged.

8. **Health endpoint**
   ```
   GET /api/market/health          -> status healthy|degraded|not_configured
   GET /api/market/session/status  -> session_state, feed_state, fingerprint (NO token)
   ```

9. **React build**
   ```
   cd frontend && npm run build
   bash trading/infrastructure/frontend/deploy.sh
   ```
   New `/market` nav item + top-bar ticker.

10. **Backend restart**
    ```
    docker compose -f docker-compose.yml -f trading/infrastructure/backend/docker-compose.prod.yml \
      up -d --build --remove-orphans
    curl -sf http://localhost:8000/api/health
    ```

11. **Production verification** (paper)
    - `GET /api/market/indices` → 4 rows; during market hours `status: live`.
    - `GET /api/market/nifty/option-chain?range=5` → ATM correct vs. spot.
    - `GET /api/market/candles/NIFTY` → 1-minute rows accumulate (~1/min).
    - Dashboard `/market` ticker updates; Market Feed shows `RUNNING`.
    - `TRADING_MODE` still `paper`; no order endpoints added.
    - Confirm PostgreSQL row growth ≈ 1/min/series, not per-tick
      (`SELECT count(*) FROM market_candles`).

## Rollback

`MARKET_DATA_ENABLED` unset → the engine does not start; the rest of the
API is unaffected. The migration can be reversed with
`alembic downgrade a1b2c3d4e5f6` (drops the 3 new tables only).

## Retention (manual)

`trading/market_data/retention.py::run_retention(db, dry_run=True)` first;
then without `dry_run` to purge candles older than
`MARKET_DATA_RETENTION_DAYS` / `OPTION_DATA_RETENTION_DAYS`. Never wired to
auto-run.
