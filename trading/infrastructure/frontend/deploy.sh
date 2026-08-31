#!/usr/bin/env bash
# Build the frontend and publish it to the S3 bucket behind CloudFront.
# Safe to run repeatedly. Reads BUCKET / DIST_ID from .deploy-outputs
# (written by provision.sh) unless they are already in the environment.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
FE="$REPO_ROOT/frontend"
[ -f "$HERE/.deploy-outputs" ] && . "$HERE/.deploy-outputs"
BUCKET="${BUCKET:?set BUCKET or run provision.sh first}"
DIST_ID="${DIST_ID:?set DIST_ID or run provision.sh first}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "building frontend (production)"
cd "$FE"
npm ci
npm run build   # vite picks up .env.production automatically

# --- guard: no secrets in the shipped bundle -------------------------
say "scanning dist/ for accidental secrets"
if grep -rEi 'AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|aws_secret_access_key' dist/ ; then
  echo "!! potential secret in build output — aborting" >&2
  exit 1
fi
# The API key is entered by the operator at runtime; it must never be
# baked in. Fail if the known env var name shows up with a value.
if grep -rE 'CONTROL_API_KEY["'\'' ]*[:=]' dist/ ; then
  echo "!! CONTROL_API_KEY reference in build output — aborting" >&2
  exit 1
fi
echo "clean."

# --- upload: hashed assets first (immutable), then the shell --------
say "syncing hashed assets (long cache)"
aws s3 sync dist/ "s3://$BUCKET/" --delete \
  --exclude "index.html" \
  --cache-control "public,max-age=31536000,immutable"

say "uploading index.html (no-cache)"
aws s3 cp dist/index.html "s3://$BUCKET/index.html" \
  --cache-control "no-cache" \
  --content-type "text/html; charset=utf-8"

# --- bust the edge cache for the shell -----------------------------
say "invalidating /"
aws cloudfront create-invalidation --distribution-id "$DIST_ID" \
  --paths "/" "/index.html" --query "Invalidation.{Id:Id,Status:Status}" --output table

say "deployed. domain:"
aws cloudfront get-distribution --id "$DIST_ID" \
  --query "Distribution.{Domain:DomainName,Status:Status}" --output table
