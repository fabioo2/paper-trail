#!/bin/sh
# In-network smoke test of the running stack (S0-AC-11, S0-AC-20, S0-AC-34).
#
# Runs inside the pinned curl container (compose service `smoke`, profile dev) on the project
# network, after `docker compose up -d --wait`. The LAN half of S0-AC-11 (another device or the
# WireGuard phone reaching http://192.168.1.10:8420) stays a manual check.
set -eu

BASE_URL="${SMOKE_BASE_URL:-http://web:8000}"
READY_TIMEOUT_S="${SMOKE_READY_TIMEOUT_S:-60}"
BODY="$(mktemp)"
HEADERS="$(mktemp)"
trap 'rm -f "$BODY" "$HEADERS"' EXIT

failures=0
pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1" >&2; failures=$((failures + 1)); }

# fetch <path>: writes body/headers, prints the status code
fetch() {
    curl --silent --show-error --max-time 10 --output "$BODY" --dump-header "$HEADERS" \
        --write-out '%{http_code}' "$BASE_URL$1" || printf '000'
}
header() { grep -i "^$1:" "$HEADERS" | head -n 1 | cut -d: -f2- | tr -d ' \r'; }
body_has() { grep -q "$1" "$BODY"; }

waited=0
until [ "$(fetch /api/health/ready)" = "200" ]; do
    if [ "$waited" -ge "$READY_TIMEOUT_S" ]; then
        fail "readiness did not return 200 within ${READY_TIMEOUT_S} s"
        cat "$BODY" >&2 || true
        exit 1
    fi
    sleep 1
    waited=$((waited + 1))
done
pass "GET /api/health/ready -> 200"
body_has '"name":"database"' && pass "readiness reports the database component" \
    || fail "readiness body lacks the database component"

status="$(fetch /api/health)"
[ "$status" = "200" ] && body_has '"status":"ok"' && pass "GET /api/health -> 200 ok" \
    || fail "GET /api/health -> $status"
case "$(header x-request-id)" in
    ????????????????????????????????) pass "responses carry a generated X-Request-ID" ;;
    *) fail "missing or malformed X-Request-ID" ;;
esac

for path in / /scan /review/anything; do
    status="$(fetch "$path")"
    if [ "$status" = "200" ] && body_has '<div id="root">' && [ "$(header cache-control)" = "no-cache" ]; then
        pass "GET $path -> SPA index.html (no-cache)"
    else
        fail "GET $path -> $status, cache-control=$(header cache-control)"
    fi
done

status="$(fetch /manifest.webmanifest)"
content_type="$(header content-type)"
if [ "$status" = "200" ] && [ "${content_type%%;*}" = "application/manifest+json" ] \
    && body_has '"start_url"' && [ "$(head -c 1 "$BODY")" = "{" ]; then
    pass "GET /manifest.webmanifest -> application/manifest+json"
else
    fail "GET /manifest.webmanifest -> $status $content_type"
fi

status="$(fetch /api/does-not-exist)"
content_type="$(header content-type)"
[ "$status" = "404" ] && [ "${content_type%%;*}" = "application/problem+json" ] \
    && pass "unknown /api path -> 404 problem+json" || fail "unknown /api path -> $status $content_type"

if [ "$failures" -gt 0 ]; then
    printf '%s smoke check(s) failed\n' "$failures" >&2
    exit 1
fi
printf 'smoke test passed\n'
