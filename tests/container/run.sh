#!/bin/sh
# Container-level acceptance checks for Stage 0 (S0-AC-07/08/09/10/12/36/39).
#
# Runs INSIDE a pinned docker CLI container (compose service `container-checks`, profile dev)
# with the Docker socket mounted and the repository mounted at its host path, so nothing is
# installed on the host. It expects the runtime image to be built already (`make build`).
#
# It starts a throwaway compose project (`paper-trail-checks`) on its own fresh data volume and
# a non-default port, so it never touches the owner's running `paper-trail` stack, and removes
# exactly that project's containers and volume on exit. The single-host check uses its own LLM
# network and a stand-in `ollama` container (never the real Ollama image), removed on exit too.
set -eu

IMAGE="paper-trail:${PAPER_TRAIL_IMAGE_TAG:-latest}"
PROJECT="paper-trail-checks"
CHECK_PORT="${PAPER_TRAIL_CHECKS_PORT:-18420}"
# 400 MB in Stage 0, 500 MB once Tesseract is in the image (PLAN S0-AC-08; the agent core
# pulled Tesseract forward from Stage 2).
IMAGE_BUDGET_MB="${PAPER_TRAIL_IMAGE_BUDGET_MB:-500}"
HEALTHY_WITHIN_S=60
SINGLE_HOST_OVERLAY="deploy/single-host/compose.override.yaml"
CHECK_LLM_NETWORK="paper-trail-checks-llm"
OLLAMA_STAND_IN="paper-trail-checks-ollama"
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

single_host_compose() {
    PAPER_TRAIL_LLM_NETWORK="$CHECK_LLM_NETWORK" checks_compose -f "$SINGLE_HOST_OVERLAY" "$@"
}

cleanup() {
    checks_compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    docker rm -f "$OLLAMA_STAND_IN" >/dev/null 2>&1 || true
    docker network rm "$CHECK_LLM_NETWORK" >/dev/null 2>&1 || true
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

# --- S0-AC-39: LLM host compose validates; models resolved -----------------------------------
llm_config="$(docker compose --env-file deploy/llm/.env.example -f deploy/llm/compose.yaml config)"
for model in qwen2.5:7b-instruct qwen2.5vl:7b nomic-embed-text; do
    case "$llm_config" in
        *"$model"*) pass "S0-AC-39 ollama-init pulls $model" ;;
        *) fail "S0-AC-39 resolved LLM compose does not mention $model" ;;
    esac
done

# --- S0-AC-36 / PLAN 2.4a: single-machine topology resolves -----------------------------------
expect "S0-AC-36 LLM stack network is named paper-trail-llm" \
    sh -c "docker compose --env-file deploy/llm/.env.example -f deploy/llm/compose.yaml config --format json | grep -q '\"name\": *\"paper-trail-llm\"'"
expect "S0-AC-36 single-host overlay attaches worker to the external paper-trail-llm network" \
    sh -c "docker compose --env-file /dev/null -f compose.yaml -f $SINGLE_HOST_OVERLAY config --format json | grep -q '\"name\": *\"paper-trail-llm\"'"

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

# --- S0-AC-36 / PLAN 2.4a: the worker reaches `ollama` by name over the shared network --------
# A stand-in HTTP server (the app image's Python) plays Ollama on a throwaway LLM network; the
# worker, started with the single-host overlay, must reach it at http://ollama:11434.
reach_ollama='import urllib.request; urllib.request.urlopen("http://ollama:11434/", timeout=2)'
docker network create "$CHECK_LLM_NETWORK" >/dev/null
docker run -d --name "$OLLAMA_STAND_IN" --network "$CHECK_LLM_NETWORK" --network-alias ollama \
    --read-only --cap-drop ALL --entrypoint python "$IMAGE" -m http.server 11434 >/dev/null
reached=false
for _ in 1 2 3 4 5 6 7 8 9 10; do
    if single_host_compose run --rm --no-deps -T --entrypoint python worker -c "$reach_ollama" \
        >/dev/null 2>&1; then
        reached=true
        break
    fi
    sleep 1
done
[ "$reached" = true ] && pass "S0-AC-36 single-host worker reaches http://ollama:11434" \
    || fail "S0-AC-36 single-host worker cannot reach http://ollama:11434"
if checks_compose run --rm --no-deps -T --entrypoint python worker -c "$reach_ollama" >/dev/null 2>&1; then
    fail "S0-AC-36 worker reaches ollama without the overlay (the check proves nothing)"
else
    pass "S0-AC-36 without the overlay the worker is isolated from the LLM network"
fi

if [ "$failures" -gt 0 ]; then
    printf '%s container check(s) failed\n' "$failures" >&2
    exit 1
fi
printf 'all container checks passed\n'
