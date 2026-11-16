#!/bin/sh
# Container-level acceptance checks for Stage 0 (S0-AC-07/08/09/10/12).
#
# Runs INSIDE a pinned docker CLI container (compose service `container-checks`, profile dev)
# with the Docker socket mounted and the repository mounted at its host path, so nothing is
# installed on the host. It expects the runtime image to be built already (`make build`).
#
# It starts a throwaway compose project (`paper-trail-checks`) on its own fresh data volume and
# a non-default port, so it never touches the owner's running `paper-trail` stack, and removes
# exactly that project's containers and volume on exit.
set -eu

IMAGE="paper-trail:${PAPER_TRAIL_IMAGE_TAG:-latest}"
PROJECT="paper-trail-checks"
CHECK_PORT="${PAPER_TRAIL_CHECKS_PORT:-18420}"
# PLAN S0-AC-08.
IMAGE_BUDGET_MB="${PAPER_TRAIL_IMAGE_BUDGET_MB:-500}"
HEALTHY_WITHIN_S=60
HERE="$(cd "$(dirname "$0")" && pwd)"

failures=0
pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1" >&2; failures=$((failures + 1)); }
expect() { # expect <description> <command...>
    description="$1"; shift
    if "$@" >/dev/null 2>&1; then pass "$description"; else fail "$description"; fi
}

# Interpolate with defaults only: ignore the developer's .env.
runtime_compose() { docker compose --env-file /dev/null -f compose.yaml "$@"; }
dev_compose() { docker compose --env-file /dev/null -f compose.yaml -f compose.dev.yaml "$@"; }
checks_compose() {
    PAPER_TRAIL_PORT="$CHECK_PORT" PAPER_TRAIL_BIND_ADDR=127.0.0.1 \
        docker compose --env-file /dev/null -p "$PROJECT" -f compose.yaml "$@"
}

cleanup() {
    checks_compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# --- S0-AC-07: one image, runtime services only by default -----------------------------------
images="$(runtime_compose config --images | sort -u)"
[ "$images" = "$IMAGE" ] && pass "S0-AC-07 migrate/web/worker share $IMAGE" \
    || fail "S0-AC-07 expected only $IMAGE, got: $images"

services="$(dev_compose config --services | sort | tr '\n' ' ')"
[ "$services" = "migrate web worker " ] && pass "S0-AC-07 dev tooling hidden behind profile dev" \
    || fail "S0-AC-07 default services are: $services"

expect "S0-AC-07 dev profile adds the tooling services" \
    sh -c "docker compose --env-file /dev/null -f compose.yaml -f compose.dev.yaml --profile dev config --services | grep -qx dev"

# --- S0-AC-10: resolved published ports ------------------------------------------------------
published="$(runtime_compose config --format json | grep -o '"host_ip": *"[^"]*"' | sort -u | tr -d ' ')"
[ "$published" = '"host_ip":"127.0.0.1"' ] && pass "S0-AC-10 web publishes on 127.0.0.1 by default" \
    || fail "S0-AC-10 published host_ip values: $published"
expect "S0-AC-10 default published port is 8420" \
    sh -c "docker compose --env-file /dev/null -f compose.yaml config --format json | grep -q '\"published\": *\"8420\"'"

# --- S0-AC-08: image budget, user, contents, built SPA ---------------------------------------
# The unpacked size, as the sum of the layers: `docker image inspect .Size` also counts the
# compressed blobs on the containerd image store, so it would differ between hosts.
size_bytes="$(docker history --human=false --format '{{.Size}}' "$IMAGE" | awk '{ total += $1 } END { printf "%d", total }')"
size_mb=$((size_bytes / 1000000))
[ "$size_mb" -lt "$IMAGE_BUDGET_MB" ] && pass "S0-AC-08 image is ${size_mb} MB (< ${IMAGE_BUDGET_MB} MB)" \
    || fail "S0-AC-08 image is ${size_mb} MB, budget ${IMAGE_BUDGET_MB} MB"

user="$(docker image inspect --format '{{.Config.User}}' "$IMAGE")"
[ "$user" = "app" ] && pass "S0-AC-08 image USER is app" || fail "S0-AC-08 image USER is '$user'"

if docker run --rm -i --network none --entrypoint python "$IMAGE" - < "$HERE/check_runtime_image.py"; then
    pass "S0-AC-08/33/38 in-image checks"
else
    fail "S0-AC-08/33/38 in-image checks (see output above)"
fi

# --- S0-AC-09: fresh volume -> migrate exits 0 -> web and worker healthy within 60 s ---------
cleanup
started="$(date +%s)"
if checks_compose up -d --no-build --wait --wait-timeout "$HEALTHY_WITHIN_S" web worker; then
    pass "S0-AC-09 web and worker healthy in $(( $(date +%s) - started )) s"
else
    fail "S0-AC-09 stack not healthy within ${HEALTHY_WITHIN_S} s"
    checks_compose logs --no-color --tail 50 >&2 || true
fi

migrate_id="$(checks_compose ps -a -q migrate)"
migrate_exit="$(docker inspect --format '{{.State.ExitCode}}' "$migrate_id" 2>/dev/null || echo missing)"
[ "$migrate_exit" = "0" ] && pass "S0-AC-09 migrate exited 0" || fail "S0-AC-09 migrate exit code: $migrate_exit"

owner="$(checks_compose exec -T web stat -c '%u:%n' /data/paper_trail.db 2>/dev/null || echo missing)"
[ "$owner" = "1000:/data/paper_trail.db" ] && pass "S0-AC-09 /data/paper_trail.db owned by UID 1000" \
    || fail "S0-AC-09 database file: $owner"

# --- S0-AC-12: hardening as actually applied by the engine -----------------------------------
for service in web worker; do
    id="$(checks_compose ps -q "$service")"
    [ -n "$id" ] || { fail "S0-AC-12 $service is not running"; continue; }
    expect "S0-AC-12 $service read-only root fs" \
        sh -c "[ \"\$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' $id)\" = true ]"
    expect "S0-AC-12 $service drops ALL capabilities" \
        sh -c "docker inspect --format '{{json .HostConfig.CapDrop}}' $id | grep -q ALL"
    expect "S0-AC-12 $service no-new-privileges" \
        sh -c "docker inspect --format '{{json .HostConfig.SecurityOpt}}' $id | grep -q no-new-privileges"
    expect "S0-AC-12 $service /tmp is tmpfs" \
        sh -c "docker inspect --format '{{json .HostConfig.Tmpfs}}' $id | grep -q '\"/tmp\"'"
    expect "S0-AC-12 $service memory limit" \
        sh -c "[ \"\$(docker inspect --format '{{.HostConfig.Memory}}' $id)\" -gt 0 ]"
    expect "S0-AC-12 $service CPU limit" \
        sh -c "[ \"\$(docker inspect --format '{{.HostConfig.NanoCpus}}' $id)\" -gt 0 ]"
    expect "S0-AC-12 $service json-file log rotation" \
        sh -c "docker inspect --format '{{.HostConfig.LogConfig.Type}} {{index .HostConfig.LogConfig.Config \"max-size\"}}' $id | grep -Eq '^json-file [0-9]+'"
    expect "S0-AC-12 $service runs as UID 1000" \
        sh -c "[ \"\$(docker exec $id id -u)\" = 1000 ]"
done

# --- S0-AC-10: what is actually listening ----------------------------------------------------
web_ports="$(docker port "$(checks_compose ps -q web)" 8000/tcp 2>/dev/null | sort -u | tr '\n' ' ')"
[ "$web_ports" = "127.0.0.1:${CHECK_PORT} " ] && pass "S0-AC-10 web bound to 127.0.0.1:${CHECK_PORT} only" \
    || fail "S0-AC-10 web published as: $web_ports"

if [ "$failures" -gt 0 ]; then
    printf '%s container check(s) failed\n' "$failures" >&2
    exit 1
fi
printf 'all container checks passed\n'
