#!/bin/sh
# Secret scan with gitleaks, run INSIDE the pinned gitleaks image (compose service `gitleaks`).
#
#   secrets-scan.sh all    [repo]   every commit in the history AND the working tree as it is
#                                   (`make secrets`); untracked files are included on purpose
#   secrets-scan.sh staged [repo]   only what is staged for the next commit (`make pre-commit`)
#
# repo defaults to /repo (the bind-mounted checkout). Paths are scanned relative to the repo
# root so the allowlist in .gitleaks.toml applies. Exits non-zero when a leak is found.
set -eu

usage() { echo "usage: $0 all|staged [repo]" >&2; exit 2; }
[ $# -ge 1 ] || usage
mode="$1"
repo="${2:-/repo}"
config="${SECRETS_SCAN_CONFIG:-/repo/.gitleaks.toml}"

cd "$repo"
scan() { gitleaks "$@" --config "$config" --no-banner --redact --verbose --exit-code 1; }

case "$mode" in
    all)
        if git rev-parse --verify --quiet HEAD >/dev/null; then
            echo "secrets: scanning git history"
            scan git .
        else
            echo "secrets: no commits yet, history scan skipped"
        fi
        echo "secrets: scanning working tree"
        scan dir .
        ;;
    staged)
        echo "secrets: scanning staged changes"
        scan git --staged .
        ;;
    *) usage ;;
esac
