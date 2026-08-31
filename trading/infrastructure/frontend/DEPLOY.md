# Stage 17 — React frontend production deployment (S3 + CloudFront)

Delivers `frontend/` as a static SPA on CloudFront, with the **same**
CloudFront distribution reverse-proxying the API to the existing EC2
backend. No ALB, no second domain, no cert on EC2.

```
                         ┌────────────────── CloudFront (HTTPS, *.cloudfront.net) ──────────────────┐
  browser ──HTTPS──▶     │  default  "/*"          → S3 origin  (private, OAC)   [SPA static files] │
                         │  "/api/*" "/strategies" │                                                │
                         │  "/health"              → EC2 origin (http-only :80)  [FastAPI/nginx]    │
                         └────────────────────────────────────────────────────────────────────────────┘
  viewer-request CloudFront Function on the default behavior only:
      extension-less path  →  rewrite to /index.html   (SPA deep-link / refresh)
```

Why this shape:

- **Private S3 + OAC** — the bucket has Block-Public-Access on, ACLs
  disabled, and a bucket policy that allows `s3:GetObject` **only** for
  `cloudfront.amazonaws.com` with `AWS:SourceArn` = this distribution. No
  public S3 website endpoint.
- **API as a second CloudFront origin** — keeps the browser 100 %
  same-origin, so the HTTPS page can call the plain-HTTP EC2 backend
  without mixed-content errors and without CORS. This is the
  "demonstrated requirement" that removes any need for an ALB.
- **Environment-specific API URL** = *relative* `/api`. `frontend/.env.production`
  sets `VITE_API_BASE_URL=` (empty) so nothing host-specific is baked
  into the JS. Point a different environment at a different backend by
  changing that origin in the distribution, not by rebuilding.
- **No secrets in JS** — the app holds no secrets; the operator types the
  `X-API-Key` at runtime and it lives only in that browser's
  `localStorage`. `deploy.sh` greps `dist/` and aborts on any key-shaped
  string.
- **Caching** —
  - `/assets/*` (content-hashed): `Cache-Control: public,max-age=31536000,immutable`, CloudFront `CachingOptimized`.
  - `index.html`: `Cache-Control: no-cache` + an invalidation on every deploy, so a release is picked up immediately.
  - `/api/*`, `/strategies`, `/health`: CloudFront `CachingDisabled` + `AllViewerExceptHostHeader` origin-request policy (forwards `X-API-Key`, query strings, body; all HTTP methods).

## Files

| file | purpose |
|---|---|
| `cloudfront-distribution-config.json` | full distribution config (placeholders: `__OAC_ID__`, `__FUNCTION_ARN__`, `__CALLER_REF__`) |
| `cloudfront-function-spa-rewrite.js` | viewer-request function, default behavior only |
| `s3-bucket-policy.json` | OAC-only read policy (placeholder: `__DISTRIBUTION_ID__`) |
| `iam-frontend-deployer-policy.json` | least-privilege policy for whoever runs provision/deploy |
| `provision.sh` | one-time: bucket + OAC + function + distribution + bucket policy |
| `deploy.sh` | repeatable: `npm ci && npm run build` → `s3 sync` (2 cache passes) → invalidation |
| `verify.sh` | curl checks against the live domain |

## Prerequisites

`aws` CLI with credentials that allow the actions in
`iam-frontend-deployer-policy.json`. The current `trading-control-cli` IAM
user does **not** have S3/CloudFront rights — attach that policy to it (or
to a dedicated `frontend-deployer` user) first:

```sh
aws iam put-user-policy --user-name trading-control-cli \
  --policy-name FrontendDeployer \
  --policy-document file://trading/infrastructure/frontend/iam-frontend-deployer-policy.json
```

Bucket name is fixed in the templates: `centralized-algo-frontend-471112713822` (ap-south-1).
CloudFront / OAC / Functions are global — run from any region.

## One-time provision

```sh
bash trading/infrastructure/frontend/provision.sh
# writes trading/infrastructure/frontend/.deploy-outputs (gitignored):
#   BUCKET, DIST_ID, DIST_ARN, DIST_DOMAIN, OAC_ID, FUNC_ARN
```

The distribution takes ~10–15 min to reach `Deployed`. Then:

## Deploy (every release)

```sh
bash trading/infrastructure/frontend/deploy.sh
```

## Verify

```sh
bash trading/infrastructure/frontend/verify.sh
```

Checks: dashboard shell loads, hashed asset has the immutable cache
header, `http://` redirects to `https://`, refresh on `/servers`
`/pnl` `/system-health` `/commands` returns the SPA shell (200),
`/api/health` + `/strategies` proxy through to the backend, and
`/api/algos` returns **401** with no key and with a bad key
(auth boundary intact).

Then open `https://<DIST_DOMAIN>/`, sign in with the `CONTROL_API_KEY`,
and confirm the PAPER banner. **No live trading controls** — the Commands
screen issues process-control only; there is no live order execution.

## Known follow-ups (not blockers for this stage)

- **EC2 origin is a public DNS name, not an Elastic IP.** CloudFront
  can't use a bare IPv4 for a custom origin, so the config uses
  `ec2-13-206-203-145.ap-south-1.compute.amazonaws.com`. If the instance
  is stopped/started its public IP (and that name) change and the API
  origin breaks. Attach an Elastic IP and update the origin `DomainName`.
- **CloudFront → EC2 is HTTP.** Fine inside AWS for paper, but for live
  put a real domain + ACM/Let's-Encrypt cert on the nginx box and switch
  the origin to `https-only`. (CloudFront won't do TLS to a bare IP or a
  self-signed cert.)
- **Custom domain + ACM** for the frontend itself (us-east-1 cert,
  `Aliases` + `ViewerCertificate`) whenever one is available.
- Access logging / WAF are off; enable if this leaves paper.
