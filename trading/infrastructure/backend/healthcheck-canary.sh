#!/bin/sh
# Emits a CloudWatch metric CentralizedAlgo/Backend HealthOK = 1|0 from the
# instance's own view of GET /api/health (checks the JSON, not just the
# HTTP code). Run every minute via cron:
#
#   * * * * * /opt/centralized-algo/app/trading/infrastructure/backend/healthcheck-canary.sh
#
# Then create an alarm on HealthOK < 1 for 3 datapoints. AWS credentials
# come from the instance role -- none on disk.
set -eu

REGION="ap-south-1"
NAMESPACE="CentralizedAlgo/Backend"
URL="http://127.0.0.1:8000/api/health"
LOG="/var/log/centralized-algo/healthcheck.log"
IID="$(curl -s --max-time 2 http://169.254.169.254/latest/meta-data/instance-id || echo unknown)"

body="$(curl -s --max-time 5 "$URL" || true)"
if printf '%s' "$body" | grep -q '"status": *"ok"' && printf '%s' "$body" | grep -q '"database": *"connected"'; then
  ok=1
else
  ok=0
fi

printf '%s HealthOK=%s %s\n' "$(date -u +%FT%TZ)" "$ok" "$body" >> "$LOG" 2>/dev/null || true

aws cloudwatch put-metric-data \
  --region "$REGION" \
  --namespace "$NAMESPACE" \
  --metric-name HealthOK \
  --unit Count \
  --value "$ok" \
  --dimensions InstanceId="$IID" >/dev/null 2>&1 || true
