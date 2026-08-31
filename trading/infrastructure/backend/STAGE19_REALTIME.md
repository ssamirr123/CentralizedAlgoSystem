# Stage 19 — realtime monitoring (WebSocket)

Adds a push stream so the dashboard reflects state within ~1s instead of
on the next 15s poll. **Polling is the automatic fallback** — every
screen still works with the socket down.

## Backend

- **`GET /api/ws`** (`trading/api/realtime/ws.py`). Auth: the access
  token is passed as a WebSocket subprotocol —
  `["cas.realtime.v1", "bearer.<jwt>"]` (also accepts `?access_token=`).
  Requires an active user with `VIEW`. Bad/absent token → an `error`
  frame then close `1008`.
- **In-process bus** (`trading/api/realtime/bus.py`): fan-out to every
  connected socket, bounded per-connection queue (1000). A client that
  falls behind is closed `1013` and expected to reconnect + REST-resync.
  Single-process only — if the backend is ever run with >1 worker /
  container, swap the bus internals for Redis pub/sub (surface is
  `subscribe`/`publish`, the swap is local).
- **Events** (`events.py`), one per state change, `{type, seq, ts, data}`
  with a process-monotonic `seq`:
  `strategy_status · heartbeat · pnl · position · trade · server_health ·
  command · alert`.
- **Publish points** (`publish.py`, best-effort, never break a request):
  - `POST /api/heartbeat` → `heartbeat` (+ `strategy_status` + `alert` on
    a status transition)
  - `POST /api/trades` → `trade`; `POST /api/positions` → `position`;
    `POST /api/pnl` → `pnl`
  - `POST /api/logs` (ERROR/WARNING) → `alert`
  - `POST /api/algo/{start,stop,restart,update}` and `GET
    /api/command/{id}` → `command` (+ `strategy_status` when the verified
    outcome syncs onto the algo)
  - `GET /api/server/status?live=true` → `server_health`
  - stale-heartbeat watcher → `alert` + `strategy_status` (STALE)
  - legacy `POST /update_strategy` is mirrored too (`heartbeat` /
    `strategy_status` / `pnl` / day-loss `alert`)
- **Liveness**: server pings every `REALTIME_PING_INTERVAL_SECONDS` (25);
  if no client frame for `REALTIME_CLIENT_TIMEOUT_SECONDS` (60) it closes
  `1001`. `REALTIME_ENABLED=false` unmounts the endpoint entirely.
- **No trading-strategy code imports the realtime module.**

### Env (optional — defaults are fine)

```dotenv
REALTIME_ENABLED=true
REALTIME_PING_INTERVAL_SECONDS=25
REALTIME_CLIENT_TIMEOUT_SECONDS=60
```

## Frontend

- `src/realtime/` — `RealtimeClient` (backoff reconnect 1s→30s + jitter,
  client ping, liveness watchdog, `seq` dedupe), `RealtimeProvider`
  (owns the one socket, patches the react-query cache per event, does one
  `invalidateQueries()` on every (re)connect = "REST is the source of
  truth"), `realtimeStore` (status + bounded alerts feed).
- While the socket is `open`, `usePollInterval()` returns `false` so
  react-query stops polling; on drop it returns 15s again.
- Top bar: a **realtime indicator** (`live` / `reconnecting…` /
  `polling (fallback)`) and an **Alerts** bell with an unread count and a
  dropdown of the session's alerts.
- `VITE_REALTIME=on|off` (default on). `off` → pure polling, no socket.

## nginx — required change on the backend box

The deployed HTTP-only nginx must forward the WebSocket upgrade. Add to
the `http {}` scope and the `location /` block (or a dedicated
`location /api/ws`):

```nginx
# http {} scope
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

# inside server {} -> location / (or location /api/ws)
proxy_http_version 1.1;
proxy_set_header Upgrade            $http_upgrade;
proxy_set_header Connection         $connection_upgrade;
proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto  $scheme;
proxy_read_timeout  3600s;
proxy_send_timeout  3600s;
```

`sudo nginx -t && sudo systemctl reload nginx`. (The repo's
`nginx/centralized-algo-backend.conf` already has this in its 443 block;
the box currently runs a trimmed HTTP-only config that needs it added.)

## CloudFront

WebSocket works through CloudFront with **no configuration change**: the
`/api/*` behavior already forwards `Upgrade`/`Connection` (origin-request
policy `AllViewerExceptHostHeader`) and has caching disabled. The browser
connects to `wss://d1135mn36rkeep.cloudfront.net/api/ws`.

## Deploy order (when approved)

1. `git pull` on the box; **update nginx** as above; `nginx -t` + reload.
2. Rebuild + restart the backend image (no new Python deps — `websockets`
   already ships with uvicorn).
3. Deploy the frontend build.
4. Verify: `wss://…/api/ws` connects with a `bearer.<jwt>` subprotocol,
   the indicator shows `live`, an action on one tab reflects on another
   within ~1s, and killing the socket falls back to polling.

## Verified (local)

Backend: `tests/api/test_realtime.py` (10 tests) + full suite green.
Real-HTTP e2e: subprotocol negotiated, `hello`, fan-out of
heartbeat/strategy_status/trade/pnl/alert with monotonic unique `seq`,
client ping→pong, bad token → `1008`. Frontend: `tsc --noEmit` + `vite
build` clean.
