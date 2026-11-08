# syntax=docker/dockerfile:1
# One image, several targets (ADR-0003):
#   frontend-dev    Node toolchain for lint, tests and the Vite dev server (compose.dev.yaml)
#   frontend-build  the production SPA bundle
#   python-dev      uv + every dependency group, project installed editable (compose.dev.yaml)
#   runtime         slim Python, production dependencies, built SPA, non-root (compose.yaml)

# Base images are literal, pinned references on their FROM lines (no ARG indirection) so
# Dependabot's docker updater can see and bump them (S0-AC-37). Each appears exactly once;
# later stages reuse them by stage name.
ARG APP_UID=1000
ARG APP_GID=1000

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

# --- frontend -----------------------------------------------------------------------------------

FROM node:24-trixie-slim AS frontend-dev
ARG APP_UID
ARG APP_GID
ENV npm_config_cache=/tmp/.npm \
    npm_config_update_notifier=false \
    npm_config_fund=false \
    npm_config_audit=false
WORKDIR /app/frontend
RUN chown ${APP_UID}:${APP_GID} /app/frontend
USER ${APP_UID}:${APP_GID}
COPY --chmod=755 scripts/fe-deps.sh /usr/local/bin/fe-deps
COPY --chown=${APP_UID}:${APP_GID} frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/tmp/.npm,uid=${APP_UID},gid=${APP_GID} fe-deps --force

FROM frontend-dev AS frontend-build
# Same-origin by default; set to serve the API from another origin (no CORS is configured).
ARG VITE_API_BASE_URL=""
COPY --chown=${APP_UID}:${APP_GID} frontend/ ./
RUN npm run build

# --- python -------------------------------------------------------------------------------------

FROM python:3.12-slim-trixie AS python-base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH
# Tesseract + English data (ADR-0010): the OCR of the runtime image, and of the dev image where
# the integration tests run it.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

FROM python-base AS python-deps
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM python-deps AS python-dev
# git is needed by pre-commit (it lists the files to check).
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
# Dependencies first, so a source edit only re-runs the cheap editable install below.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-groups --no-install-project
# The repository is bind-mounted over /app at run time; the editable install points at
# /app/src, so the live source is used while /opt/venv stays in the image. Tools run as
# `uv run --frozen --no-sync <tool>`, which never writes to /opt/venv.
COPY README.md ./
COPY src/ src/
COPY migrations/ migrations/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-groups

FROM python-base AS runtime
ARG APP_UID
ARG APP_GID
RUN groupadd --gid ${APP_GID} app \
    && useradd --uid ${APP_UID} --gid app --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin app \
    && mkdir /data \
    && chown app:app /data
WORKDIR /app
COPY --from=python-deps /opt/venv /opt/venv
# Install the project as a regular (non-editable) wheel. uv and the sources are only mounted
# for this step, so neither lands in the image; the wheel carries the migration scripts.
# --reinstall-package: uv's cache keys a local project's wheel on pyproject.toml only, so
# without it a source change would reinstall the stale wheel from the cache mount.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=/app/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/app/uv.lock \
    --mount=type=bind,source=README.md,target=/app/README.md \
    --mount=type=bind,source=src,target=/app/src \
    --mount=type=bind,source=migrations,target=/app/migrations \
    uv sync --frozen --no-dev --no-editable --reinstall-package paper-trail
COPY --from=frontend-build /app/frontend/dist /app/static
# Build metadata last: a new commit SHA or version must not invalidate the layers above.
ARG GIT_SHA=""
ARG APP_VERSION=unknown
ARG SOURCE_URL=""
LABEL org.opencontainers.image.title="Paper Trail" \
      org.opencontainers.image.description="Receipt scanner and price tracker (web + worker)" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="${SOURCE_URL}"
ENV PAPER_TRAIL_BUILD_SHA=${GIT_SHA}
USER app
EXPOSE 8000
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=4).status != 200)"]
ENTRYPOINT ["paper-trail"]
CMD ["serve"]
