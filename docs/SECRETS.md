# Secrets & Credential Handling (Stage 20)

## Rule: what may live in tracked source

**MAY** be in code / committed config:

- API **endpoint URLs**, hostnames, ports
- symbol names, exchange codes, instrument tokens
- non-secret defaults, feature flags, timeouts
- `TRADING_MODE=paper` and other safe development defaults
- `.env.example` templates with **blank** placeholder values

**MUST NOT** be in any tracked file (source, config, test, docs, CI):

- broker API keys / secrets / access tokens — AngelOne `apikey`, `clientid`, `mpin`, TOTP secret; ICICI Breeze `BREEZE_API_KEY` / `BREEZE_SECRET_KEY` / `BREEZE_SESSION_TOKEN`
- `CONTROL_API_KEY`, `AUTH_SECRET_KEY`
- database passwords / DSNs containing a password
- AWS access keys (there are none — see below)
- JWT signing secret, refresh tokens, password hashes
- Telegram bot tokens

Enforced by `tests/security/test_no_secrets_in_source.py` and the
`.gitleaks.toml` rules run in CI (`.github/workflows/secret-scan.yml`).

## Where credentials actually come from

### Development

`.env` / `.env.local` at the repo root (both git-ignored). Copy from
`.env.example` / `trading/.env.example` and fill in real values locally.

### Production — intended hierarchy

```
        AWS Secrets Manager
   (breeze/*, angelone/*, backend/*)
                 │
                 ▼
        EC2 IAM instance role
  (secretsmanager:GetSecretValue on those ARNs)
                 │
                 ▼
   backend / strategy process env
   (systemd EnvironmentFile, or the process
    reads Secrets Manager at boot)
                 │
                 ▼
      trading/core/config.Settings
      trading/algos/*/config._angel_creds()
                 │
                 ▼
   BrokerClient  /  MarketDataProvider
```

- The EC2 machines **do not** store long-lived AWS access keys. AWS calls
  (Lambda invoke, SSM, Secrets Manager, S3) authenticate via the **EC2
  instance role** (`TradingEC2SSMRole` for the backend). `.env` on a box
  never contains `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.
- ICICI Breeze already supports `BREEZE_SECRET_ID` — set it to a Secrets
  Manager secret `{"api_key","secret_key","session_token"}` and the
  backend reads it at boot (`trading/market_data/session.py`).
- The Breeze **session token is regenerated daily** by the operator (web
  login) and pushed via `POST /api/market/session` (admin) — it is
  process-local; for restart-survival also update the Secrets Manager
  secret. It is never returned by any API or written to any log.

## The three real strategies

`CombinedVwapNifty`, `DoubleStraddelAlgo`, `Vwap_Algo_Nifty_hedge` read
AngelOne credentials via `config._angel_creds()`, which resolves from:

1. process environment (`ANGELONE_CLIENT_ID`, `ANGELONE_API_KEY`,
   `ANGELONE_MPIN` (or `ANGELONE_PASSWORD`), `ANGELONE_TOTP_SECRET`)
2. a git-ignored `trading/.env` at the repo root (fallback)

Missing credentials yield **empty strings** — the strategy logs a broker
error and does not trade. It never crashes on missing config, and there
are no literal values in the tracked file.

## Logging

`trading/common/logger.redact_text()` + `SecretRedactionFilter` scrub
credential-shaped substrings (`key=value`, `Bearer <jwt>`, DSN passwords,
`AKIA…`, PEM blocks) from every message and rendered traceback that
passes through the canonical JSON logger. `trading/market_data/session.py`
routes its API-facing error text through the same redactor and uses a
fixed safe message for auth failures.

## Rotation runbook (if a secret is exposed)

1. **Rotate at the provider first** (before any Git-history rewrite):
   - AngelOne: regenerate the SmartAPI app key/secret, change the MPIN,
     reset TOTP/2FA.
   - Breeze: regenerate API key/secret; the session token rotates daily
     anyway.
   - `CONTROL_API_KEY` / `AUTH_SECRET_KEY`: generate new random values,
     update `/etc/centralized-algo/backend.env` + every consumer
     (Lambda env, strategy `.env`), restart.
2. Update Secrets Manager / `.env` with the new values.
3. Only then rewrite Git history (coordinate: `git filter-repo`, force
   push `main` + feature branches, re-clone every checkout including the
   strategy box).
