# Stage 18 — production auth & security (deploy notes)

Adds per-user auth + RBAC + audit to the `/api/*` surface. The shared
`X-API-Key` still works but is now a **fixed, VIEW-only machine identity**
— it can no longer start/stop/restart a process or reach `/api/admin/*`.

## What changed at runtime

| Area | Before | After |
|---|---|---|
| Human auth | none (shared key) | `POST /api/auth/login` → JWT access token (15 min) + httpOnly `cas_refresh` cookie (7 d, rotating) |
| Authorization | all-or-nothing | RBAC: `VIEW / START / STOP / RESTART / TRADING_CONTROL / ADMIN`; roles `viewer < trader < operator < admin` |
| Machine key | full control | `VIEW` + telemetry ingest only |
| CSRF | n/a | double-submit `X-CSRF-Token` on `/api/auth/refresh` + `/logout`; access token is Bearer-only |
| CORS | none | `AUTH_ALLOWED_ORIGINS` allow-list, `allow_credentials=true`, no `*` |
| Cookies | n/a | `Secure` + `SameSite=Strict` (set `AUTH_COOKIE_SECURE=false` only for plain-http local dev) |
| Rate limit | per API key | per identity (`user:<id>` / service); plus a 5-per-5-min lockout on `/api/auth/login` |
| Audit | `commands` table only | `audit_log` table: every auth event, control action, admin action, and permission denial. `GET /api/admin/audit` (ADMIN). |

## New env vars — add to `/etc/centralized-algo/backend.env`

```dotenv
# REQUIRED in production. >= 32 chars. Rotating it logs everyone out.
AUTH_SECRET_KEY=<openssl rand -base64 48>

# Exact browser origin(s) allowed to call the API with credentials.
# The CloudFront distribution + any dev origin. Comma-separated, no trailing slash.
AUTH_ALLOWED_ORIGINS=https://d1135mn36rkeep.cloudfront.net

# Cookies. Keep true in production (HTTPS via CloudFront).
AUTH_COOKIE_SECURE=true

# Optional tuning (defaults shown)
AUTH_ACCESS_TTL_MINUTES=15
AUTH_REFRESH_TTL_DAYS=7
AUTH_LOGIN_MAX_ATTEMPTS=5
AUTH_LOGIN_WINDOW_SECONDS=300

# One-time bootstrap: set BOTH, deploy once, then REMOVE them. Creates a
# single admin (must change password on first login) iff no users exist.
AUTH_BOOTSTRAP_ADMIN_USERNAME=admin
AUTH_BOOTSTRAP_ADMIN_PASSWORD=<strong one-time password>
```

`CONTROL_API_KEY` stays exactly as-is (strategy processes still post
heartbeats/logs with it).

## Deploy steps (coordinated — backend + frontend together)

1. `git pull` on the box; rebuild the image (adds `bcrypt`, `PyJWT`).
2. Add the env vars above. Set `AUTH_ALLOWED_ORIGINS` to the real
   CloudFront domain.
3. `alembic upgrade head` — creates `users`, `auth_sessions`, `audit_log`
   (revision `a1b2c3d4e5f6`). Nothing existing is touched.
4. Start the stack. On first boot the bootstrap admin is created; the log
   line says so.
5. **Remove `AUTH_BOOTSTRAP_ADMIN_*`** and restart, or leave them — they
   no-op once a user exists.
6. Deploy the matching frontend build (`trading/infrastructure/frontend/deploy.sh`).
   The new UI logs in with username/password.
7. Log in as the bootstrap admin, change the password, create real
   accounts (`Administration → Users`). New users default to `viewer`.

### CLI alternative for user management

```sh
python -m trading.api.admin_cli create-user --username alice --role operator
python -m trading.api.admin_cli list-users
python -m trading.api.admin_cli set-role --username alice --role admin
python -m trading.api.admin_cli reset-password --username alice
python -m trading.api.admin_cli deactivate --username alice
```

## CloudFront

No distribution change needed. The `/api/*` behavior already uses the
`AllViewerExceptHostHeader` origin-request policy (forwards `Authorization`,
`Cookie`, query strings) and `CachingDisabled` (so `Set-Cookie` passes
through to the viewer untouched).

## Not in this stage

- Live trading is still **not implemented**. `TRADING_MODE=paper` stays;
  no order-execution path was added. The brief said "do not implement
  live trading until this stage is complete" — that gate is now in place
  (TRADING_CONTROL permission + audit), enabling live remains a separate,
  explicit future step.
- Legacy `POST /update_strategy` / `GET /strategies` / `GET /health`
  remain unauthenticated for backward compatibility (Streamlit dashboard
  + older strategy code). They are slated for removal, not hardening.
