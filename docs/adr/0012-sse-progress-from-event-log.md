# 12. Server-sent events fed from a database event log

Date: 2026-11-06

## Status

Accepted

## Context

The Scan screen shows a live status card. Progress is produced by the worker process, but the browser talks to the web process. No message broker exists.

## Decision

The worker appends rows to a `receipt_events` table (`id`, `receipt_id`, `stage`, `message`, `created_at`). `GET /api/receipts/{id}/events` streams them as SSE (`sse-starlette`), polling the table every 500 ms, honouring `Last-Event-ID`, sending a keep-alive comment every 15 s, and closing after a terminal stage. The frontend uses `EventSource` and falls back to polling `GET /api/receipts/{id}` if SSE fails.

## Consequences

- No broker; events are durable and replayable after reconnects.
- Up to 500 ms latency, invisible next to OCR and LLM latency.

## Amendment (2026-11-06): events come from the agent tracer

The agent core (built before the rest of Stage 2) records every graph step through a `Tracer` port, which feeds the CLI output and a JSON trace file. When the worker runs the graph, an `EventLogTracer` adapter writes the `receipt_events` rows from the same trace events (node events mapped to `ProcessingStage`). There is one source of progress events; nodes contain no event-log code.
