# Paper Trail documentation

Setting up and a short tour are in the main [README](../README.md). The rest is here.

## Guides

- [Using the app](using-the-app.md): the phone web app, screen by screen, and the JSON API.
- [How it works](how-it-works.md): architecture, the agents' graph, photos from the phone,
  matching products, product lookups and guesses, what gets saved, tools, allowlists and MCP.
- [The command line](cli.md): importing receipts from JSON, processing photos with the agents,
  corrections and resuming, naming products by their codes.
- [Running and maintaining it](operations.md): the API key, cost and the monthly budget, Make
  targets, configuration, backups and restoring, upgrading.
- [Data and privacy](privacy.md): what stays on your server and what is sent out.
- [Contributing](../CONTRIBUTING.md): the test strategy, layering rules and how to add a
  migration.

## Design decisions (ADRs)

- [ADR-0001](adr/0001-record-architecture-decisions.md): Record architecture decisions
- [ADR-0002](adr/0002-hexagonal-architecture.md): Hexagonal architecture with Protocol ports
- [ADR-0003](adr/0003-containerized-runtime-and-tooling.md): Containerized runtime and development tooling
- [ADR-0004](adr/0004-sqlite-single-database.md): One SQLite database with FTS5 and sqlite-vec
- [ADR-0005](adr/0005-database-backed-job-queue.md): Database-backed job queue with leases and a GPU availability probe
- [ADR-0006](adr/0006-money-as-integer-cents.md): Money as integer minor units, quantities as Decimal
- [ADR-0007](adr/0007-openapi-generated-typescript-client.md): TypeScript API client generated from the OpenAPI spec
- [ADR-0008](adr/0008-deterministic-supervisor.md): Deterministic supervisor routing in LangGraph
- [ADR-0009](adr/0009-ollama-structured-output.md): Ollama via OpenAI-compatible endpoint with JSON-schema output
- [ADR-0010](adr/0010-tesseract-first-ocr.md): Tesseract-first OCR with vision-model fallback
- [ADR-0011](adr/0011-mask-sensitive-data-before-persistence.md): Mask sensitive data before persistence
- [ADR-0012](adr/0012-sse-progress-from-event-log.md): Server-sent events fed from a database event log
- [ADR-0013](adr/0013-tool-registry-and-mcp-adapter.md): Single tool registry with per-role allowlists; MCP as an adapter
- [ADR-0014](adr/0014-spa-served-by-fastapi.md): React SPA served by FastAPI from the same origin
- [ADR-0015](adr/0015-data-local-source-public.md): Data stays on private hardware; source is public on GitHub
- [ADR-0016](adr/0016-two-host-deployment.md): Two-host deployment
- [ADR-0017](adr/0017-llm-transcribes-code-computes.md): The LLM transcribes, code computes
- [ADR-0018](adr/0018-product-identity-from-codes.md): Products are identified by the code on the receipt; descriptions come only from data
- [ADR-0019](adr/0019-claude-reads-receipts.md): Claude reads receipts; no OCR, no local models, no vectors
- [ADR-0020](adr/0020-host-and-origin-checks.md): Host and Origin checks instead of a login

Each ADR's status says whether it still holds: ADR-0019 superseded 0009, 0010 and 0016 (the
local models) and amended 0013 and 0015.

## Plans and history

- [Roadmap](ROADMAP.md): the high-level roadmap and what is built.
- [Proposal](proposal.md): the original project proposal.
- [Implementation plan](PLAN.md): the staged plan with acceptance criteria.
- Feature plans in [`plans/`](plans/): [agent core](plans/agent-core.md),
  [barcode lookup](plans/barcode-lookup.md),
  [discounts and paid prices](plans/discounts-and-paid-prices.md),
  [extraction accuracy](plans/extraction-accuracy.md).
- Test plans in [`tests/`](tests/): each plan's requirements mapped to the tests that cover them.

## Screenshots

The README's screenshots are in [`screenshots/`](screenshots/), taken at phone size from a
throwaway stack holding only the made-up receipts in [`screenshots/demo/`](screenshots/demo/)
(imported with `receipts import`; the No Frills one does not add up on purpose, so the Review
screen has something in it).
