# 7. TypeScript API client generated from the OpenAPI spec

Date: 2026-11-06

## Status

Accepted

## Context

The API contract should be defined once. Hand-written TypeScript types drift from the Pydantic schemas.

## Decision

- FastAPI's OpenAPI document is the contract. `paper-trail openapi` writes it to `frontend/openapi.json`.
- `openapi-typescript` generates `frontend/src/api/schema.d.ts`; `openapi-fetch` provides a typed, dependency-light client (`frontend/src/api/client.ts`).
- Both generated files are committed. `make api-check` regenerates them in containers and fails on any diff; it runs in `make check`.
- Route operation ids are explicit and stable (`operation_id` on every route).

## Consequences

- Changing a Pydantic schema without regenerating the client fails `make check`.
- Generated files appear in diffs; reviewers skip them.
