# Paper Trail: every target runs in Docker (ADR-0003). Nothing is installed on the host.
# `make` lists the targets; `make ci` is the full local gate.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# A git-ignored compose.override.yaml holds per-host changes, e.g. the single-machine overlay from
# deploy/single-host (PLAN 2.4a). On GitHub Actions compose.ci.yaml adds the GHA build cache
# (PLAN 2.6); locally it is unused.
DC := docker compose -f compose.yaml -f compose.dev.yaml$(if $(wildcard compose.override.yaml), -f compose.override.yaml)$(if $(GITHUB_ACTIONS), -f compose.ci.yaml)
RUN := $(DC) run --rm
UVRUN := uv run --frozen --no-sync
# `make process`: a file or folder under PAPER_TRAIL_RECEIPTS_DIR (default: all of it).
RECEIPTS ?= .

export PAPER_TRAIL_UID ?= $(shell id -u)
export PAPER_TRAIL_GID ?= $(shell id -g)
export GIT_SHA ?= $(shell git rev-parse --short HEAD 2>/dev/null)
export APP_VERSION ?= $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)

BASE_IMAGES := python:3.12-slim-trixie node:24-trixie-slim

.PHONY: help build up down logs ps restart shell fe-shell lock migrate revision \
	lint format typecheck test test-fast test-unit test-integration \
	fe-install fe-deps fe-lint fe-format fe-typecheck fe-test fe-build fe-dev \
	api-client api-check secrets smoke container-checks pre-commit hooks check ci \
	llm-config llm-up llm-down process pt graph mcp-up mcp-down outdated clean

help: ## List the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --- runtime stack --------------------------------------------------------------------------

build: ## Build the runtime image and the dev tooling images
	$(DC) build migrate
	$(DC) --profile dev build dev frontend

up: ## Start the runtime stack (migrate, web, worker) in the background
	$(DC) up -d

down: ## Stop the runtime stack (the data volume is kept)
	$(DC) down

logs: ## Follow the runtime stack's logs
	$(DC) logs -f

ps: ## Show the runtime stack's containers
	$(DC) ps

restart: ## Restart web and worker
	$(DC) restart web worker

migrate: ## Apply the migrations baked into the built image (`make build migrate` after adding one)
	$(RUN) migrate

# --- development shells and dependencies -----------------------------------------------------

shell: ## Open a shell in the Python dev container
	$(RUN) dev bash

fe-shell: ## Open a shell in the frontend dev container
	$(RUN) frontend bash

lock: ## Re-resolve Python dependencies into uv.lock
	$(RUN) dev uv lock

revision: ## Create a hand-written migration: make revision m="message" [id=0002]
	$(RUN) dev $(UVRUN) alembic revision -m "$(m)" $(if $(id),--rev-id $(id))

# --- backend checks ---------------------------------------------------------------------------

lint: ## Lint and format-check Python, then check the layering contracts
	$(RUN) dev $(UVRUN) ruff check
	$(RUN) dev $(UVRUN) ruff format --check
	$(RUN) dev $(UVRUN) lint-imports

format: ## Fix lint findings and format Python
	$(RUN) dev $(UVRUN) ruff check --fix
	$(RUN) dev $(UVRUN) ruff format

typecheck: ## Type-check Python (mypy --strict)
	$(RUN) dev $(UVRUN) mypy

# Both run the tests in parallel with pytest-xdist; pytest-cov combines the workers' coverage
# (and their child processes') into one report. At most 6 workers, never one per CPU (`-n auto`):
# the host's 8 cores also serve the owner's media services, which must stay responsive.
PYTEST_WORKERS := 6

test: ## Run every Python suite in parallel with branch coverage (fails under 90 %)
	$(RUN) dev $(UVRUN) pytest -n $(PYTEST_WORKERS) --cov --cov-report=term --cov-report=xml --cov-fail-under=90

test-fast: ## Day-to-day: unit, contract and integration suites in parallel, no coverage
	$(RUN) dev $(UVRUN) pytest -n $(PYTEST_WORKERS) -m "not e2e and not repo"

test-unit: ## Run the unit and contract suites
	$(RUN) dev $(UVRUN) pytest -m "not integration and not e2e and not repo"

test-integration: ## Run the integration suite
	$(RUN) dev $(UVRUN) pytest -m integration

# --- frontend ---------------------------------------------------------------------------------

# The frontend tooling keeps node_modules in the `frontend_node_modules` volume, which Docker
# seeds from the image only while it is empty. `fe-deps` (in the image, see Dockerfile) runs
# `npm ci` when package-lock.json differs from the lock that filled the volume, so every target
# below sees current dependencies after a lock change; `fe-install` always reinstalls.
fe-install: ## Reinstall frontend dependencies from package-lock.json (npm ci)
	$(RUN) frontend fe-deps --force

fe-deps: ## Reinstall frontend dependencies only if package-lock.json changed
	$(RUN) frontend fe-deps

fe-lint: fe-deps ## Lint (ESLint) and format-check (Prettier) the frontend
	$(RUN) frontend npx eslint --max-warnings=0 .
	$(RUN) frontend npx prettier --check .

fe-format: fe-deps ## Format the frontend with Prettier
	$(RUN) frontend npx prettier --write .

fe-typecheck: fe-deps ## Type-check the frontend (tsc, strict)
	$(RUN) frontend npx tsc -b

fe-test: fe-deps ## Run frontend tests with coverage (Vitest)
	$(RUN) frontend npx vitest run --coverage

fe-build: fe-deps ## Build the production SPA bundle (Vite)
	$(RUN) frontend npm run build

fe-dev: fe-deps ## Start the Vite dev server on :5173 (proxies /api to web)
	$(DC) --profile dev up -d web vite

# --- API contract (ADR-0007) ------------------------------------------------------------------

api-client: fe-deps ## Regenerate frontend/openapi.json and src/api/schema.d.ts from the backend
	$(RUN) dev $(UVRUN) paper-trail openapi --output frontend/openapi.json
	$(RUN) frontend npx openapi-typescript openapi.json --output src/api/schema.d.ts

api-check: fe-deps ## Fail if the committed OpenAPI spec or TS schema drifted from the backend
	$(RUN) -T dev $(UVRUN) paper-trail openapi \
		| $(RUN) -T frontend sh -ec '\
			cat > /tmp/openapi.json; \
			node_modules/.bin/openapi-typescript /tmp/openapi.json --output /tmp/schema.d.ts; \
			diff -u openapi.json /tmp/openapi.json; \
			diff -u src/api/schema.d.ts /tmp/schema.d.ts'

# --- gates ------------------------------------------------------------------------------------

secrets: ## Self-test the secret scan, then scan git history and the working tree (gitleaks)
	$(RUN) --entrypoint /bin/sh gitleaks /repo/tests/secrets/selftest.sh
	$(RUN) gitleaks all

smoke: ## Start the stack and smoke-test it from inside the network (leaves it running)
	$(DC) up -d --wait web worker
	$(RUN) smoke

container-checks: ## Inspect the built image and a throwaway stack (size budget, user, hardening)
	$(RUN) container-checks tests/container/run.sh

pre-commit: fe-lint ## Run the pre-commit hooks on every file and scan staged changes for secrets
	$(RUN) dev $(UVRUN) pre-commit run --all-files
	$(RUN) gitleaks staged

hooks: ## Point git at .githooks (pre-commit runs `make pre-commit`)
	git config core.hooksPath .githooks

check: lint typecheck test fe-lint fe-typecheck fe-test fe-build api-check secrets ## Fast gate: lint, types, tests, frontend, API contract, secrets

ci: build check smoke container-checks ## Full gate: build images, check, smoke-test, image budget

# --- agent core (docs/plans/agent-core.md 4.1) ---------------------------------------------------
# The run name is RUN_NAME, not RUN: RUN is the compose macro above.

process: ## Process receipts: make process RECEIPTS=<file|dir> [RUN_NAME=<run>] [ARGS="<flags>"]
	$(RUN) cli process "/receipts/$(RECEIPTS)" $(if $(RUN_NAME),--run $(RUN_NAME)) $(ARGS)

pt: ## Run any paper-trail command in the cli service: make pt ARGS="review list"
	$(RUN) cli $(ARGS)

graph: ## Print the agents' graph as Mermaid
	$(RUN) cli graph --mermaid

mcp-up: ## Serve the agents' tools over MCP on 127.0.0.1:8421 (/mcp/<role>/)
	$(DC) --profile mcp up -d mcp

mcp-down: ## Stop and remove the MCP server only (web and worker keep running)
	$(DC) --profile mcp rm --stop --force mcp

# --- LLM host (run these on the GPU PC, ADR-0016) ------------------------------------------------
# A Windows LLM host runs the same `docker compose` commands directly (README), not Make.

llm-config: ## Validate the LLM host compose file
	docker compose -f deploy/llm/compose.yaml config --quiet

llm-up: ## Start Ollama and pull the configured models (on the LLM host)
	docker compose -f deploy/llm/compose.yaml --env-file deploy/llm/.env up -d

llm-down: ## Stop Ollama (models are kept in their volume)
	docker compose -f deploy/llm/compose.yaml --env-file deploy/llm/.env down

# --- maintenance ------------------------------------------------------------------------------

outdated: ## Report newer Python, npm and base-image versions (changes nothing)
	$(RUN) dev uv tree --outdated --depth 1 --frozen
	$(RUN) frontend sh -c 'npm outdated || [ $$? -eq 1 ]'
	docker run --rm -v "$(CURDIR)/scripts:/scripts:ro" --entrypoint /bin/sh curlimages/curl:8.23.0 /scripts/base-image-tags.sh $(BASE_IMAGES)

clean: ## Remove caches, coverage and build output (never the data volume)
	$(RUN) dev sh -c 'rm -rf .mypy_cache .ruff_cache .pytest_cache .import_linter_cache htmlcov coverage.xml .coverage .coverage.*'
	$(RUN) frontend sh -c 'rm -rf dist coverage'
