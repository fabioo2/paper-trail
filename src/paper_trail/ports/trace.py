"""The per-receipt trace of agents, tools and model calls, and where it is written
(agent-core plan 2.2 and 4.3, AC-A-09)."""

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from paper_trail.domain.enums import AgentRole, ReceiptStatus, ReviewReason
from paper_trail.domain.models import JsonValue
from paper_trail.domain.trace import TraceEvent, TraceKind


class Tracer(Protocol):
    """Events belong to the receipt of the innermost ``bind``; ``emit`` outside one is an
    error. ``seq`` counts from 1 per receipt and continues across binds (a resumed run appends).
    Summaries longer than 90 characters are cut; card numbers are masked in the summary and in
    every string of ``data``."""

    def emit(  # noqa: PLR0913 - one argument per caller-supplied TraceEvent field
        self,
        agent: AgentRole,
        kind: TraceKind,
        name: str,
        summary: str,
        *,
        data: Mapping[str, JsonValue] | None = None,
        duration_ms: int | None = None,
    ) -> TraceEvent: ...

    def events(self, receipt_id: int) -> tuple[TraceEvent, ...]: ...

    def bind(self, receipt_id: int) -> AbstractContextManager[None]: ...


@dataclass(frozen=True, slots=True)
class RunConfig:
    """How a run was configured; recorded in every trace so experiment arms can be compared."""

    models: Mapping[str, str]  # role of the model ("text", "vision", "embed") -> model name
    prompt_versions: Mapping[str, str]  # prompt name -> version
    examples: bool
    feedback: bool
    max_text_attempts: int
    max_vision_attempts: int
    enrichment: bool  # new products are described after matching (GRAPH__ENRICHMENT)
    transport: str  # how tools are called ("inprocess")


@dataclass(frozen=True, slots=True)
class ReceiptRun:
    """One receipt's run through the graph, as it ends up in its trace file."""

    receipt_id: int
    source_name: str
    image_sha256: str | None
    started_at: datetime
    finished_at: datetime
    status: ReceiptStatus
    review_id: int | None
    review_reason: ReviewReason | None
    resumed_from: str | None  # the node a resumed run continued after
    text_attempts: int
    vision_attempts: int
    events: Sequence[TraceEvent]


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One invocation over a batch of receipt files."""

    receipts: int
    completed: int
    needs_review: int
    failed: int
    skipped: int
    llm_calls: int
    mean_text_attempts: float


class TraceWriter(Protocol):
    """Writes the traces of one named run (``runs/<run>/``) where people can read them."""

    def write_receipt(self, run: ReceiptRun) -> str:
        """Write one receipt's trace; returns where it was written."""
        ...

    def write_summary(self, summary: RunSummary) -> str:
        """Write the batch summary; returns where it was written."""
        ...
