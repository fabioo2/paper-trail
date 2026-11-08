#!/bin/sh
# Proves scripts/secrets-scan.sh catches a planted secret in each place it claims to look
# (working tree, staged changes, history) and passes a clean tree, using the same config.
#
# Runs INSIDE the pinned gitleaks image (`make secrets` runs it before the real scan). The
# fake token is generated at run time, so this file never contains anything that looks like
# a secret and the real scan of the repository stays clean.
set -eu

SCAN="/repo/scripts/secrets-scan.sh"
export HOME=/tmp
workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT INT TERM

failures=0
expect() { # expect <description> <wanted exit status: 0|nonzero> <command...>
    description="$1"; wanted="$2"; shift 2
    if "$@" >"$workdir/output" 2>&1; then status=0; else status=$?; fi
    if { [ "$wanted" = 0 ] && [ "$status" -eq 0 ]; } \
        || { [ "$wanted" = nonzero ] && [ "$status" -ne 0 ]; }; then
        printf 'ok   %s\n' "$description"
    else
        printf 'FAIL %s (exit %s)\n' "$description" "$status" >&2
        cat "$workdir/output" >&2
        failures=$((failures + 1))
    fi
}

repo="$workdir/repo"
mkdir "$repo"
git() { command git -C "$repo" -c user.name=selftest -c user.email=selftest@example.invalid "$@"; }
git init --quiet
printf 'print("hello")\n' > "$repo/app.py"
git add app.py
git commit --quiet -m "clean"

token="ghp_$(head -c 4096 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c 36)"
plant() { printf 'GITHUB_TOKEN = "%s"\n' "$token" > "$repo/$1"; }

expect "clean repository passes" 0 sh "$SCAN" all "$repo"

mkdir "$repo/node_modules"
plant node_modules/vendored.js
expect "allowlisted gitignored path (node_modules) is skipped" 0 sh "$SCAN" all "$repo"
rm -r "$repo/node_modules"

plant untracked.py
expect "untracked file in the working tree is caught" nonzero sh "$SCAN" all "$repo"
rm "$repo/untracked.py"

plant staged.py
git add staged.py
expect "staged file is caught by the staged scan" nonzero sh "$SCAN" staged "$repo"

git commit --quiet -m "leak"
git rm --quiet staged.py
git commit --quiet -m "remove the leak"
expect "secret removed from the tree but still in history is caught" nonzero sh "$SCAN" all "$repo"

if [ "$failures" -gt 0 ]; then
    printf '%s secrets self-test(s) failed\n' "$failures" >&2
    exit 1
fi
printf 'secrets self-test passed\n'
