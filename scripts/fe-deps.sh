#!/bin/sh
# fe-deps [--force]: install frontend dependencies from package-lock.json with `npm ci`.
#
# Installed in the frontend-dev image as /usr/local/bin/fe-deps and run from /app/frontend.
# node_modules lives in a named volume that Docker seeds from the image only while the volume
# is empty, so after a lock change it would keep serving stale packages. A copy of the lock that
# filled node_modules is kept next to the packages; without --force, `npm ci` runs only when
# package-lock.json differs from it.
set -eu

stamp=node_modules/.paper-trail-package-lock.json

if [ "${1:-}" != "--force" ] && cmp -s package-lock.json "$stamp"; then
    echo "fe-deps: node_modules matches package-lock.json"
    exit 0
fi

npm ci
cp package-lock.json "$stamp"
