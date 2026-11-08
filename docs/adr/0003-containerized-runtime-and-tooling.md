# 3. Containerized runtime and development tooling

Date: 2026-11-06

## Status

Accepted

## Context

The host is a home server running other self-hosted services (media server, DNS, home automation). The owner requires that nothing is installed on the host: no uv, Python packages, Node, or Tesseract. CI should run exactly what developers run.

## Decision

- One multi-stage `Dockerfile` with targets: `frontend-dev` (Node toolchain), `frontend-build` (builds the SPA), `python-dev` (uv + all dev dependencies), `runtime` (slim Python, production deps, built SPA, Tesseract from Stage 2, non-root user).
- `compose.yaml` holds runtime services (`migrate`, `web`, `worker`; later `mcp`, `backup`), all from the single `paper-trail` image.
- `compose.dev.yaml` adds tooling services (`dev`, `frontend`, `vite`, `gitleaks`) under the `dev` profile, with the source bind-mounted and dependency directories (`/opt/venv`, `node_modules`) kept in the image or named volumes so the bind mount does not shadow them. Tooling containers run as the host UID/GID so generated files are owned by the developer.
- The `Makefile` is a thin wrapper over `docker compose ... run --rm`. CI calls the same Make targets; `make ci` is the full local gate.
- Base images are pinned to a language minor version and Debian release (`python:3.12-slim-trixie`, `node:24-trixie-slim`); uv is copied from its official image at a pinned version.

## Consequences

- Reproducible tooling on any machine with Docker; zero host footprint.
- First runs are slower (image builds); mitigated by layer caching and BuildKit cache mounts.
- Editor integrations (language servers) need the dev container or must tolerate missing host packages; documented in CONTRIBUTING.
