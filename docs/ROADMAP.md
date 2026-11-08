# Paper Trail: Roadmap and Architecture Decisions

Agreed with the project owner on 2026-11-06. This is the high-level roadmap; `docs/PLAN.md` holds the detailed implementation plan derived from it.

## Non-negotiable quality bar

Interview-grade software: DRY, KISS, SOLID, standard practices, no shortcuts. Every stage ends in a working, tested, mergeable state. Tooling and a local CI gate (`make ci`) from day one.

**Data stays on the owner's hardware; code is public (decided 2026-11-06).** The repo is published on GitHub with CI and Dependabot so anyone can set it up. Receipts, images, the database and secrets never leave the owner's machines. No CDNs, no telemetry. At runtime the only outbound traffic is the app host calling Ollama on the LLM host over the LAN.

**Deployment: three apps, two places.** App host (home server): `web` (FastAPI API + React SPA) and `worker` (orchestration, OCR, everything except the model). LLM host (GPU PC, RTX 3080): Ollama in Docker with GPU access. They talk over the LAN; the owner reaches the app from outside via WireGuard. Both are Docker Compose projects in the repo, and a single GPU machine can run both. The GPU PC runs Windows (Docker Desktop + WSL2). The SPA is served by the app host only; the API base URL stays a build-time setting (default same-origin) but no CORS or external frontend hosting is planned.

## Environment

- Host: Ubuntu home server, Intel i7-6700 (8 threads), 16 GB RAM, Docker 29 + Compose v5.
- **EVERYTHING RUNS IN DOCKER CONTAINERS (owner requirement).** Nothing is installed on the host: no uv, no Node, no Python packages, no Tesseract. Runtime services (web, worker, MCP server) are compose services. Development tooling (tests, lint, type-check, formatting, Alembic, frontend dev server, Vitest, OpenAPI client generation) also runs in containers via dedicated dev images / compose profiles (e.g. a `dev` target in the multi-stage Dockerfile and a `docker-compose.dev.yml` or `dev` profile with bind-mounted source). The Makefile is a thin wrapper over `docker compose run --rm ...`. CI runs the same containerized commands.
- LLM host: a separate desktop PC with an RTX 3080 (10 GB VRAM) running Ollama. It is NOT always on. No Wake-on-LAN. The server must queue jobs and probe availability.
- Access: LAN and WireGuard only. Never exposed to the internet. The phone reaches the app at the server's LAN/WireGuard address (planned friendly name via local DNS).
- Single user.

## Architecture: hexagonal (ports and adapters), one Python package

The domain and use cases know nothing about SQLite, Ollama, Tesseract, LangGraph or FastAPI. Every external dependency sits behind a small `Protocol` (port) with one adapter. Fakes implement the same ports in tests.

```
src/paper_trail/
  domain/        Receipt, LineItem, Product, PriceObservation, Category enum, value objects, pure rules (arithmetic check, PAN masking)
  ports/         Protocols: OcrPort, LlmPort, EmbeddingPort, ReceiptRepository, ProductRepository, VectorIndex, JobQueue, ReviewQueue, Clock
  adapters/      sqlite/ (repos, FTS5, sqlite-vec, migrations), ollama/, tesseract/, filesystem/
  application/   use cases: SubmitReceipt, ProcessJob, ResolveReview, SearchItems, PriceHistory
  agents/        LangGraph state, nodes (supervisor, extraction, validation, matching, enrichment), prompts/, graph builder
  tools/         plain functions the agents call + per-role allowlist registry; the MCP server is a thin adapter over this
  interfaces/    web/ (FastAPI JSON API + static SPA), cli/ (Typer), mcp/ (MCP server on the SDK's low-level Server), worker/ (queue loop)
  config.py      pydantic-settings; one Settings object, injected
frontend/        React SPA (see below)
```

## Backend stack

- Python 3.12, `uv` for deps and lockfile, `src/` layout, `pyproject.toml` only.
- FastAPI serving a JSON API plus the built SPA static files.
- SQLAlchemy 2.0 Core + Alembic migrations. sqlite-vec and FTS5 loaded on connect. WAL mode. One database file.
- LangGraph with a typed state and the SqliteSaver checkpointer in the same database.
- Ollama on the 3080 PC via its OpenAI-compatible endpoint with JSON-schema structured output. Models: Qwen2.5 7B instruct (text), Qwen2.5-VL 7B (vision fallback), nomic-embed-text (embeddings).
- Tesseract as default OCR inside the container; vision model only after validation fails twice.
- Worker and web run from the SAME image as two compose services (`web`, `worker`). One image, one database.
- Image versioning (agreed 2026-11-06): base images pinned to a version line, e.g. `python:3.12-slim`, `node:22-alpine` (not `:latest`, not digests). Dependabot opens upgrade PRs that CI tests; `make outdated` gives the same report locally.
- Quality gates: ruff (lint+format), mypy strict, pytest + coverage, pre-commit, GitHub Actions CI that calls the same Make targets as the local `make ci` gate. Structured JSON logging with a per-receipt correlation id.
- Docs: README with architecture diagram, short ADRs (docs/adr/), CONTRIBUTING with the test strategy.

## Frontend stack (the app must look and feel like a real product on a phone)

- React 19 + Vite + TypeScript (strict), in `frontend/`.
- Tailwind CSS + shadcn/ui components (owned as source).
- TanStack Query for server state (caching, optimistic updates, polling).
- TanStack Router for type-safe routes.
- react-hook-form + zod for forms. Recharts for price history.
- TypeScript API client GENERATED from FastAPI's OpenAPI spec (contract defined once in Python).
- PWA manifest + icons (installable, full screen). No service worker / offline (would need HTTPS; uploads need the server anyway).
- Mobile-first, bottom tab bar: Scan, Receipts, Search, Review. Dark mode. Proper empty states, skeleton loaders, error toasts, a design-token file.
- Camera: `<input type="file" accept="image/*" capture="environment">` (native camera, no HTTPS needed).
- Product-feel flows:
  - Scan: native camera, preview with crop/rotate, upload progress, then a live status card ("queued, reading, checking, matching, done") via server-sent events. If the GPU PC is off the card says so honestly.
  - Review: receipt image beside extracted fields, tap to fix, totals re-check live, one tap to approve.
  - Search: instant results, filter chips (store, category, date), item page with price history chart and "cheapest at" callout.
- Build: multi-stage Dockerfile; Node stage builds the bundle, Python stage serves it. Frontend tooling: ESLint, Prettier, Vitest, all part of `make ci`.

## Testing strategy

Unit tests use in-memory fakes for every port (no network, no GPU). Integration tests run against a temporary SQLite file. Agent nodes are tested with a scripted fake LLM returning canned JSON so retry loops and review routing are deterministic. The eval harness (Stage 7) is the only thing that needs the real model.

## Stages

0. Foundations: repo, package layout, tooling, local CI gate, multi-stage Dockerfile, compose (web + worker), Settings, logging, health endpoint, Alembic wired with an empty first migration, ADRs, frontend scaffold (Vite/React/TS/Tailwind/shadcn/ESLint/Prettier/Vitest) with a placeholder shell and bottom tab bar. Done when `make check` passes and the container answers on the LAN.
1. Domain and storage: domain models and pure rules (arithmetic validation, Luhn-based card masking, category enum). Schema: receipts, line_items, products, product_aliases, price_observations, jobs, review_items, corrections, FTS5 and vec tables. Repository adapters with integration tests. Done when a receipt can be inserted and queried end to end without AI.
2. Ingestion without AI: Scan flow in the SPA (camera input, preview, upload progress, live status card via SSE), upload endpoint, image stored on disk, job enqueued, worker loop with GPU-availability probe and backoff, OCR adapter, masking applied before storage. Done when a phone photo over WireGuard shows its OCR text in the UI.
3. Extraction and validation graph: LLM port + Ollama adapter with structured output. Extraction and validation nodes, supervisor routing, retry cap, vision re-read fallback, LangGraph interrupt parking a receipt in the review queue. Versioned prompt files with few-shot hard examples; receipt text always inside a delimited data block. Done when a clean receipt lands validated and a damaged one lands in review.
4. Matching, enrichment, memory: embedding port, vector index, alias memory. Matching: exact alias hit, then similarity search with threshold, then new product. Enrichment constrained to the category enum. Review corrections write back to alias memory. Done when the same item on two receipts from two stores links to one product.
5. MCP server and tool scoping: expose the tool functions over MCP (the SDK's low-level server) with a per-role allowlist enforced in the registry. Done when the server runs standalone and agents fail loudly on out-of-role tool calls.
6. Full UI and design pass: review queue, hybrid search (FTS5 + vector, reciprocal rank fusion), item page with price history, polish, dark mode, empty states.
7. Evaluation: labeling format + CLI, 50 labeled receipts, metrics (field accuracy, line-item F1, match accuracy, category accuracy, review rate), single-prompt baseline, JSON + markdown report, re-run later to show memory improving.
8. Hardening and polish: nightly SQLite backup to a host folder of your choice, upload size/type limits, retry caps, coverage pass, README with diagram and screenshots, synthetic demo dataset.

## Development process (agent pipeline)

Per stage: a planner produces/refines the detailed plan, a test author writes failing tests from the plan, an implementer makes them pass, a reviewer reviews and the implementer fixes. Commits only when the owner asks. No Claude co-author lines in commits.

## Build order (revised 2026-11-06)

A course progress report is due Sunday 2026-11-08 (evening). The rubric grades experiments with the framework, prompting, multi-agent orchestration, memory, tool use and safeguards, so the agent core is built before the remaining UI work. Quality bar unchanged.

1. Stage 0 (foundations).
2. Stage 1 in full (domain and storage).
3. Agent core = plan Stages 3, 4 and 5, plus the OCR adapter and card masking pulled forward from Stage 2. Driven from the CLI on real receipt files. Priority inside it: (a) supervisor + extraction + validation with feedback retries, code-checked totals, masking, review routing; (b) tool registry with per-role allowlists, then the MCP server; (c) matching with alias memory + checkpointer resume; (d) vision fallback and enrichment.
4. Remaining Stage 2 (upload UI, live status, GPU-availability queue with the overnight GPU window, default 02:00-06:00), then Stages 6, 7, 8.
