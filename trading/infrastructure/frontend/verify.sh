#!/usr/bin/env bash
# Post-deploy checks against the live CloudFront domain.
#   DIST_DOMAIN=dxxxx.cloudfront.net bash verify.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/.deploy-outputs" ] && . "$HERE/.deploy-outputs"
DOMAIN="${DIST_DOMAIN:?set DIST_DOMAIN}"
BASE="https://$DOMAIN"
pass=0; fail=0
ok()  { printf '  \033[1;32mPASS\033[0m  %s\n' "$1"; pass=$((pass+1)); }
no()  { printf '  \033[1;31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); }

echo "== dashboard loads =="
body="$(curl -s "$BASE/")"
echo "$body" | grep -q '<div id="root">' && ok "GET / returns the SPA shell" || no "GET / missing #root"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/"); [ "$code" = 200 ] && ok "GET / -> 200" || no "GET / -> $code"

asset="$(printf '%s' "$body" | grep -o '/assets/[^"]*\.js' | head -1)"
if [ -n "$asset" ]; then
  h="$(curl -s -D - -o /dev/null "$BASE$asset")"
  echo "$h" | grep -qiE 'cache-control:.*(immutable|max-age=31536000)' && ok "asset $asset has long immutable cache" || no "asset cache-control: $(echo "$h" | grep -i cache-control)"
  echo "$h" | grep -q '200' && ok "asset 200" || no "asset not 200"
else
  no "could not find a hashed /assets/*.js in index.html"
fi

echo "== HTTPS =="
red=$(curl -s -o /dev/null -w '%{http_code}' "http://$DOMAIN/")
[ "$red" = 301 ] || [ "$red" = 302 ] || [ "$red" = 308 ] && ok "http:// -> $red redirect to https" || no "http:// -> $red (expected redirect)"
curl -s -o /dev/null "$BASE/" && ok "https:// serves" || no "https:// failed"

echo "== SPA route refresh =="
for r in /servers /pnl /system-health /commands; do
  c=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$r")
  t="$(curl -s "$BASE$r" | grep -c '<div id="root">')"
  { [ "$c" = 200 ] && [ "$t" -ge 1 ]; } && ok "refresh $r -> 200 + SPA shell" || no "refresh $r -> $c (shell hits=$t)"
done

echo "== API requests work (same-origin proxy) =="
hc="$(curl -s "$BASE/api/health")"
echo "$hc" | grep -q '"status"' && ok "GET /api/health proxied to backend: $hc" || no "GET /api/health: $hc"
c=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/strategies"); [ "$c" = 200 ] && ok "GET /strategies -> 200" || no "GET /strategies -> $c"

echo "== authentication boundary preserved =="
c=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/algos"); [ "$c" = 401 ] && ok "GET /api/algos (no key) -> 401" || no "GET /api/algos (no key) -> $c"
c=$(curl -s -o /dev/null -w '%{http_code}' -H 'X-API-Key: wrong' "$BASE/api/algos"); [ "$c" = 401 ] && ok "GET /api/algos (bad key) -> 401" || no "GET /api/algos (bad key) -> $c"

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
