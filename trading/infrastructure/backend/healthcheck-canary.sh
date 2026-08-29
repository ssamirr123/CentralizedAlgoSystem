#!/bin/sh
# Emits a CloudWatch metric CentralizedAlgo/Backend HealthOK = 1|0 from the
# instance's own view of GET /api/health (checks the JSON, not just the
# HTTP code). Run every minute via cron:
#
#   * * * * * /usr/local/bin/centralized-algo-healthcheck
#
# Then create an alarm on HealthOK < 1 for 3 datapoints. AWS credentials
# come from the EC2 instance role -- none on disk. Needs
# cloudwatch:PutMetricData (CloudWatchAgentServerPolicy covers it).
set -eu

NAMESPACE="CentralizedAlgo/Backend"
URL="http://127.0.0.1:8000/api/health"
LOG="/var/log/centralized-algo/healthcheck.log"

# IMDSv2 (token required on Amazon Linux 2023)
TOKEN="$(curl -s --max-time 2 -X PUT \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 300' \
  http://169.254.169.254/latest/api/token || true)"
imds() {
  curl -s --max-time 2 -H "X-aws-ec2-metadata-token: $TOKEN" \
    "http://169.254.169.254/latest/meta-data/$1" || true
}
IID="$(imds instance-id)"
REGION="$(imds placement/region)"
: "${REGION:=ap-south-1}"

body="$(curl -s --max-time 5 "$URL" || true)"
if printf '%s' "$body" | grep -q '"status": *"ok"' && printf '%s' "$body" | grep -q '"database": *"connected"'; then
  ok=1
else
  ok=0
fi

printf '%s HealthOK=%s iid=%s %s\n' "$(date -u +%FT%TZ)" "$ok" "${IID:-none}" "$body" >> "$LOG" 2>/dev/null || true

[ -n "$IID" ] || exit 0   # no instance id -> can't dimension the metric; skip quietly

aws cloudwatch put-metric-data \
  --region "$REGION" \
  --namespace "$NAMESPACE" \
  --metric-name HealthOK \
  --unit Count \
  --value "$ok" \
  --dimensions InstanceId="$IID" >/dev/null 2>&1 || true
