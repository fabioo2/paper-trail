# 16. Two-host deployment

Date: 2026-11-06

## Status

Accepted

## Context

The owner's home server has no GPU. A desktop PC with an RTX 3080 is on the same LAN but is not always on. Other people setting up Paper Trail may have one GPU machine or two machines. The owner's GPU PC runs Windows.

## Decision

- Two roles, each its own Docker Compose project in the repo: the app host (`compose.yaml`: `migrate`, `web`, `worker`) and the LLM host (`deploy/llm/compose.yaml`: `ollama` with GPU, `ollama-init` to pull models).
- The hosts communicate over the LAN through one setting, `PAPER_TRAIL_OLLAMA__BASE_URL`. Docker networks do not span machines, so no overlay network or Swarm is used.
- The worker probes the LLM host and leaves jobs queued while it is off (ADR-0005).
- One GPU machine can run both projects. They share a Docker network instead of the LAN: the LLM project's network is named `paper-trail-llm`, and `deploy/single-host/compose.override.yaml` attaches the app's `worker` to it, so the base URL is `http://ollama:11434` and Ollama publishes nothing beyond `127.0.0.1`. (Amended 2026-11-06: the first draft used `host.docker.internal`, which on Linux resolves to the bridge gateway and cannot reach a port published on `127.0.0.1`.)
- The SPA is a static bundle with a build-time API base URL (default same-origin). Serving the SPA from FastAPI (ADR-0014) is the deployment; no CORS is configured. Hosting the frontend elsewhere was considered and dropped by the owner (2026-11-06); the configurable base URL is kept because it is standard practice and the Vite dev server needs it anyway.

## Consequences

- Good: works for one or two machines with the same files.
- Good: the model host is replaceable (another GPU box, or a different LLM adapter behind `LlmPort`).
- Bad: Ollama has no authentication. Its port must be bound to the LAN address and firewalled to the app host; the README states this.
