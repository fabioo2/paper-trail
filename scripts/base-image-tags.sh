#!/bin/sh
# Report newer Docker Hub tags for pinned base images (`make outdated`). Read-only.
#
# Usage: base-image-tags.sh python:3.12-slim-trixie node:24-trixie-slim
# For each image it lists the newest tags that share the pinned tag's variant suffix, so a
# new patch release (python 3.12.x) or a new major line (node 26) is visible at a glance.
set -eu

for image in "$@"; do
    repository="${image%%:*}"
    tag="${image#*:}"
    variant="${tag#*-}"              # 3.12-slim-trixie -> slim-trixie
    case "$repository" in */*) path="$repository" ;; *) path="library/$repository" ;; esac
    printf '%s (pinned %s), newest *-%s tags:\n' "$repository" "$tag" "$variant"
    curl --silent --show-error --fail --max-time 20 \
        "https://hub.docker.com/v2/repositories/${path}/tags?page_size=100&ordering=last_updated&name=${variant}" \
        | tr ',' '\n' | sed -n 's/^.*"name":"\([0-9][^"]*\)".*$/\1/p' \
        | grep -E "^[0-9][0-9.]*-${variant}\$" | sort -t. -k1,1nr -k2,2nr -k3,3nr | head -n 5 \
        | sed 's/^/  /'
done
