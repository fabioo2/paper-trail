# 1. Record architecture decisions

Date: 2026-11-06

## Status

Accepted

## Context

Paper Trail is built in stages by a pipeline of agents (planner, test author, implementer, reviewer) and reviewed by the owner. Decisions made early (storage, queueing, orchestration) constrain later stages, and the reasoning behind them must survive between sessions and agents.

## Decision

We record significant architecture decisions as short Architecture Decision Records in `docs/adr/`, using Michael Nygard's format (Title, Status, Context, Decision, Consequences). Files are numbered `NNNN-kebab-title.md` and never renumbered. A decision that is replaced is marked `Superseded by ADR-NNNN`, not deleted.

## Consequences

- Reviewers and later agents can check work against recorded decisions instead of re-deriving them.
- Every PR that changes a recorded decision must add or supersede an ADR.
- Small overhead per decision; trivial choices are not recorded.
