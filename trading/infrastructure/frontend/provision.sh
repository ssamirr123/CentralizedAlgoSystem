#!/usr/bin/env bash
# One-time provisioning of the frontend delivery stack:
#   private S3 bucket (OAC-only) + CloudFront distribution (SPA + API proxy)
#
# Idempotent-ish: re-running reuses an existing bucket / OAC / function,
# but ALWAYS creates a new distribution — run it once, then use deploy.sh.
#
# Requires AWS creds with the actions in iam-frontend-deployer-policy.json.
# CloudFront + OAC + Functions are global; the bucket is regional.
set -euo pipefail

REGION="${REGION:-ap-south-1}"
ACCOUNT_ID="${ACCOUNT_ID:-471112713822}"
BUCKET="${BUCKET:-centralized-algo-frontend-${ACCOUNT_ID}}"
OAC_NAME="${OAC_NAME:-centralized-algo-frontend-oac}"
FUNC_NAME="${FUNC_NAME:-centralized-algo-spa-rewrite}"
# The API-proxy origin. CloudFront cannot use a bare IP for a custom
# origin. The backend now has Elastic IP 13.232.95.211, so its public
# DNS name is stable across stop/start — pinned here. If the EIP is ever
# changed, update this (or unset it to re-resolve live from the
# instance's current public IP).
BACKEND_INSTANCE_ID="${BACKEND_INSTANCE_ID:-i-0f344752a1ca2811b}"
API_ORIGIN_DNS="${API_ORIGIN_DNS:-ec2-13-232-95-211.ap-south-1.compute.amazonaws.com}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/.deploy-outputs"
TMPD="$HERE/.provision-tmp"
mkdir -p "$TMPD"
trap 'rm -rf "$TMPD"' EXIT

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# --- resolve the API-proxy origin DNS name ---------------------------
if [ -z "${API_ORIGIN_DNS:-}" ]; then
  ip="$(aws ec2 describe-instances --region "$REGION" --instance-ids "$BACKEND_INSTANCE_ID" \
    --query 'Reservations[].Instances[].PublicIpAddress' --output text)"
  [ -n "$ip" ] && [ "$ip" != "None" ] || { echo "backend $BACKEND_INSTANCE_ID has no public IP" >&2; exit 1; }
  API_ORIGIN_DNS="ec2-$(echo "$ip" | tr . -).${REGION}.compute.amazonaws.com"
fi
say "API origin -> $API_ORIGIN_DNS"

# The AWS CLI here is native Windows aws.exe under Git Bash, which cannot
# read MSYS-style paths (/c/...) in file:// / fileb:// args. Convert.
winpath() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"
  else echo "$1" | sed -E 's|^/([a-zA-Z])/|\1:/|'; fi
}
fileb() { printf 'fileb://%s' "$(winpath "$1")"; }
filet() { printf 'file://%s'  "$(winpath "$1")"; }

# --- S3 bucket -------------------------------------------------------------
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  say "bucket s3://$BUCKET already exists — reusing"
else
  say "creating private bucket s3://$BUCKET ($REGION)"
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
fi

aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" >/dev/null

aws s3api put-bucket-ownership-controls --bucket "$BUCKET" \
  --ownership-controls "Rules=[{ObjectOwnership=BucketOwnerEnforced}]" >/dev/null

aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}' >/dev/null

aws s3api put-bucket-tagging --bucket "$BUCKET" --tagging \
  'TagSet=[{Key=project,Value=centralized-algo},{Key=component,Value=frontend}]' >/dev/null
say "bucket locked down (BPA on, ACLs off, SSE-S3, tagged)"

# --- Origin Access Control ----------------------------------------------
OAC_ID="$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='$OAC_NAME'].Id | [0]" --output text 2>/dev/null || true)"
if [ -z "$OAC_ID" ] || [ "$OAC_ID" = "None" ]; then
  say "creating Origin Access Control '$OAC_NAME'"
  OAC_ID="$(aws cloudfront create-origin-access-control --origin-access-control-config \
    "Name=$OAC_NAME,Description=centralized-algo frontend,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=s3" \
    --query "OriginAccessControl.Id" --output text)"
else
  say "reusing OAC $OAC_ID"
fi

# --- CloudFront Function (SPA rewrite) ---------------------------------
FUNC_ARN="$(aws cloudfront list-functions \
  --query "FunctionList.Items[?Name=='$FUNC_NAME'].FunctionMetadata.FunctionARN | [0]" --output text 2>/dev/null || true)"
if [ -z "$FUNC_ARN" ] || [ "$FUNC_ARN" = "None" ]; then
  say "creating CloudFront Function '$FUNC_NAME'"
  aws cloudfront create-function --name "$FUNC_NAME" \
    --function-config "Comment=SPA rewrite for centralized-algo frontend,Runtime=cloudfront-js-2.0" \
    --function-code "$(fileb "$HERE/cloudfront-function-spa-rewrite.js")" >/dev/null
fi
ETAG="$(aws cloudfront describe-function --name "$FUNC_NAME" --query "ETag" --output text)"
aws cloudfront update-function --name "$FUNC_NAME" --if-match "$ETAG" \
  --function-config "Comment=SPA rewrite for centralized-algo frontend,Runtime=cloudfront-js-2.0" \
  --function-code "fileb://$HERE/cloudfront-function-spa-rewrite.js" >/dev/null 2>&1 || true
ETAG="$(aws cloudfront describe-function --name "$FUNC_NAME" --query "ETag" --output text)"
aws cloudfront publish-function --name "$FUNC_NAME" --if-match "$ETAG" >/dev/null
FUNC_ARN="$(aws cloudfront describe-function --name "$FUNC_NAME" \
  --query "FunctionSummary.FunctionMetadata.FunctionARN" --output text)"
say "function published: $FUNC_ARN"

# --- CloudFront distribution ------------------------------------------
CFG="$TMPD/distribution-config.json"
sed -e "s|__OAC_ID__|$OAC_ID|g" \
    -e "s|__FUNCTION_ARN__|$FUNC_ARN|g" \
    -e "s|__API_ORIGIN_DNS__|$API_ORIGIN_DNS|g" \
    -e "s|__CALLER_REF__|centralized-algo-frontend-$(date +%s)|g" \
    "$HERE/cloudfront-distribution-config.json" > "$CFG"

say "creating CloudFront distribution"
DIST_JSON="$(aws cloudfront create-distribution --distribution-config "$(filet "$CFG")" \
  --query "Distribution.{Id:Id,Arn:ARN,Domain:DomainName}" --output json)"
DIST_ID="$(echo "$DIST_JSON" | grep -o '"Id": *"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
DIST_ARN="$(echo "$DIST_JSON" | grep -o '"Arn": *"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
DIST_DOMAIN="$(echo "$DIST_JSON" | grep -o '"Domain": *"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"

# --- Bucket policy: allow ONLY this distribution via OAC --------------
POL="$TMPD/bucket-policy.json"
sed "s|__DISTRIBUTION_ID__|$DIST_ID|g" "$HERE/s3-bucket-policy.json" > "$POL"
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$(filet "$POL")" >/dev/null
say "bucket policy bound to distribution $DIST_ID"

# --- Persist outputs for deploy.sh ----------------------------------
cat > "$OUT" <<EOF
# generated by provision.sh $(date -u +%FT%TZ)
BUCKET=$BUCKET
DIST_ID=$DIST_ID
DIST_ARN=$DIST_ARN
DIST_DOMAIN=$DIST_DOMAIN
OAC_ID=$OAC_ID
FUNC_ARN=$FUNC_ARN
EOF

say "DONE"
cat "$OUT"
echo
echo "Distribution is deploying (~10-15 min). Then:"
echo "  BUCKET=$BUCKET DIST_ID=$DIST_ID bash $HERE/deploy.sh"
echo "  DIST_DOMAIN=$DIST_DOMAIN bash $HERE/verify.sh"
