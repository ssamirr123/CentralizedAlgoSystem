# Trading Control Center — Frontend

New React + TypeScript UI for the Centralized Algo System. Talks to the
**existing** FastAPI `/api/*` backend — no backend contract changes.

The Streamlit dashboard (`dashboard/streamlit_app.py`) is untouched and
still runs; this app is additive.

## Stack

- React 18 + TypeScript, Vite
- React Router (12 screens + login)
- TanStack Query (polling / cache)
- Plain CSS (`src/index.css`), no component library

## Screens

Login · Dashboard · Servers · Strategies · Algo Status · Heartbeats · P&L ·
Positions · Trades · Commands · Logs · Risk · System Health

## Paper vs Live

- A build targets one mode via `VITE_TRADING_MODE` (`paper` | `live`).
  Anything that is not exactly `live` is treated as **paper** (fail safe).
- The mode is shown **at all times**: a coloured stripe + pill in the top
  bar, on the login card, and restated inside every command-confirmation
  dialog.
- **No live order execution is implemented.** The Commands screen only
  issues process-control actions (start / stop / restart / update) against
  the backend; it never places or cancels orders. `LIVE_EXECUTION_ENABLED`
  in `src/lib/config.ts` is hard-wired `false`.

## Auth (Stage 18)

Per-user login: username + password → a short-lived JWT access token
(kept **in memory only**) plus an httpOnly `cas_refresh` cookie the
backend sets. On load the app silently calls `POST /api/auth/refresh`;
`client.ts` also does one transparent refresh-and-retry on a 401.

RBAC: `AuthContext.hasPermission(perm)` gates nav entries (`routes.tsx`
`permission`), the Commands screen's action buttons
(`START/STOP/RESTART/TRADING_CONTROL`), and the Administration screen
(`ADMIN` — users + audit log). A forced-password-change account is
redirected to `/change-password`.

No secret is stored in the browser beyond the httpOnly cookie the backend
controls; the non-httpOnly `cas_csrf` cookie is only a double-submit CSRF
token.

## Local development

```sh
cd frontend
npm install
cp .env.example .env.local     # then edit VITE_API_PROXY_TARGET / VITE_TRADING_MODE
npm run dev                     # http://localhost:5173
```

`npm run dev` proxies `/api`, `/strategies`, `/health` to
`VITE_API_PROXY_TARGET` (default `http://13.232.95.211`, the backend's
Elastic IP) so the browser
makes same-origin requests — the backend needs no CORS changes.

## Build

```sh
npm run build      # tsc --noEmit && vite build  ->  dist/
npm run preview    # serve dist/ on http://localhost:4173
```

## Production hosting (later milestone)

Serve `dist/` as static files behind the same nginx that fronts the API
(`trading/infrastructure/backend/nginx/`), so `/api/*` stays same-origin.
Set `VITE_API_BASE_URL` only if the UI is hosted somewhere the API is not
reachable same-origin (and then the backend must send CORS headers for
that origin).

## Layout

```
src/
  api/         client (fetch + X-API-Key), endpoints, types, react-query hooks
  auth/        authStore (localStorage) + AuthContext
  components/  Layout, StatusBadge, TradingModeBadge, ConfirmDialog, AlgoPicker, States
  lib/         config (env), format (IST dates, ₹, staleness)
  pages/       one file per screen
  routes.tsx   nav + route table
```
