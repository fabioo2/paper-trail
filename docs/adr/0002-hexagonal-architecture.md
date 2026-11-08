# 2. Hexagonal architecture with Protocol ports

Date: 2026-11-06

## Status

Accepted

## Context

The app depends on several volatile or slow externals: SQLite with extensions, Ollama on a PC that is often off, Tesseract, LangGraph, FastAPI, an MCP SDK. Unit tests must run without network or GPU, and agent retry/review routing must be deterministic under test.

## Decision

One Python package (`src/paper_trail`) organised as ports and adapters:

- `domain/`: frozen dataclasses, enums and pure rules. Imports only the standard library.
- `ports/`: `typing.Protocol` interfaces, one per external capability (repositories, queues, OCR, LLM, embeddings, vector index, clock, image store, health probes). Imports only `domain`.
- `application/`: use cases that depend on ports only.
- `adapters/`: one implementation per port (sqlite, ollama, tesseract, filesystem).
- `agents/`, `tools/`: LangGraph graph and tool functions; depend on ports, not adapters.
- `interfaces/`: FastAPI, Typer CLI, worker loop, MCP server. Thin; call use cases.
- The `paper_trail.bootstrap` package is the composition root that wires adapters to ports from `Settings`: `bootstrap/__init__.py` the application core (database, use cases, health), `bootstrap/agents.py` the agent core (LangGraph, the MCP SDK, the Ollama and Tesseract adapters), imported only by the commands that run agents.

Pydantic is used at the boundaries (settings, HTTP schemas, LLM output schemas), not in the domain. Layering is enforced by `import-linter` contracts in `make lint`. Tests use in-memory fakes that implement the same Protocols, and a shared port-contract test suite runs against both the fake and the real adapter.

## Consequences

- Swapping an adapter (e.g. a different OCR engine) touches one module plus the composition root.
- Mapping code exists between domain dataclasses, SQLAlchemy rows and Pydantic schemas. Accepted as the cost of the boundary.
- Layer violations fail `make check` rather than relying on review.
