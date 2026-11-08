# 8. Deterministic supervisor routing in LangGraph

Date: 2026-11-06

## Status

Accepted

## Context

The proposal describes a supervisor that decides whether to retry, re-read with vision, or send a receipt to review. Letting an LLM make that decision adds latency, cost and nondeterminism to the part of the system that enforces guardrails.

## Decision

The supervisor is a LangGraph node whose routing is a pure function of the typed state (validation result, attempt counters, confidence, whether vision was used). LLMs are used only inside specialist nodes (extraction, matching confirmation, enrichment). Retry caps and thresholds come from `Settings`. Review is a LangGraph `interrupt()`; resolving a review resumes the same thread with `Command(resume=...)` from the `SqliteSaver` checkpoint.

## Consequences

- Routing is exhaustively unit-testable with a scripted fake LLM.
- The "agentic" behaviour lives in specialists and feedback loops, which is where model judgement adds value.
- New routing behaviour requires code changes, not prompt changes. Intentional.

## Amendment (2026-11-08): the first attempt is supervisor policy too (ADR-0017)

The edge after `read` is conditional: `first_attempt(effective_limits(...))` in `agents/routing.py`, the same pure policy the supervisor routes with, sends a photo with a poor OCR read to `extract_vision` first (traced as a supervisor ROUTE event before any model call). The supervisor gives the first vision attempt a clean prompt (no feedback from the text attempts) and a vision retry only the issues of the previous vision answer. See ADR-0017.
