"""JSON trace files on the data volume: ``paper-trail.trace/v1`` (agent-core plan 4.3).

``runs/<run>/traces/receipt-<id>-<UTC>.json`` per receipt and ``runs/<run>/summary.json`` per
invocation, each written atomically. A trace is as sensitive as the database: it lives only on
the data volume and is never logged (ADR-0011). ``trace.schema.json`` beside this module is the
committed JSON schema of ``TraceDocument``.
"""

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.domain.trace import TraceEvent, TraceKind
from paper_trail.ports.trace import ReceiptRun, RunConfig, RunSummary

SCHEMA_ID: Final = "paper-trail.trace/v1"
SCHEMA_FILE = Path(__file__).with_name("trace.schema.json")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TraceConfig(_Strict):
    models: dict[str, str]
    prompt_versions: dict[str, str]
    examples: bool
    feedback: bool
    max_text_attempts: int
    max_vision_attempts: int
    enrichment: bool
    transport: str


class TraceOutcome(_Strict):
    status: str
    review_id: int | None
    review_reason: str | None
    resumed_from: str | None


class TraceTotals(_Strict):
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    tool_calls: int
    tool_denied: int
    text_attempts: int
    vision_attempts: int


class TraceEventOut(_Strict):
    seq: int
    at: datetime
    agent: AgentRole
    kind: TraceKind
    name: str
    summary: str
    duration_ms: int | None
    data: dict[str, JsonValue]


class TraceDocument(_Strict):
    schema_: Literal["paper-trail.trace/v1"] = Field(default=SCHEMA_ID, alias="schema")
    run: str
    receipt_id: int
    source_name: str
    image_sha256: str | None
    started_at: datetime
    finished_at: datetime
    config: TraceConfig
    outcome: TraceOutcome
    totals: TraceTotals
    events: list[TraceEventOut]


class RunSummaryDocument(_Strict):
    run: str
    receipts: int
    completed: int
    needs_review: int
    failed: int
    skipped: int
    llm_calls: int
    mean_text_attempts: float


class JsonTraceWriter:
    """Implements ``paper_trail.ports.trace.TraceWriter`` for the run in ``run_dir``."""

    def __init__(self, run_dir: Path, run: str, config: RunConfig) -> None:
        self._run_dir = run_dir
        self._run = run
        self._config = TraceConfig(
            models=dict(config.models),
            prompt_versions=dict(config.prompt_versions),
            examples=config.examples,
            feedback=config.feedback,
            max_text_attempts=config.max_text_attempts,
            max_vision_attempts=config.max_vision_attempts,
            enrichment=config.enrichment,
            transport=config.transport,
        )

    def write_receipt(self, run: ReceiptRun) -> str:
        document = TraceDocument(
            run=self._run,
            receipt_id=run.receipt_id,
            source_name=run.source_name,
            image_sha256=run.image_sha256,
            started_at=run.started_at,
            finished_at=run.finished_at,
            config=self._config,
            outcome=TraceOutcome(
                status=run.status.value,
                review_id=run.review_id,
                review_reason=None if run.review_reason is None else run.review_reason.value,
                resumed_from=run.resumed_from,
            ),
            totals=_totals(run),
            events=[_event(event) for event in run.events],
        )
        stamp = run.finished_at.strftime("%Y%m%dT%H%M%SZ")
        path = self._run_dir / "traces" / f"receipt-{run.receipt_id}-{stamp}.json"
        _write_atomically(path, document.model_dump_json(by_alias=True, indent=2))
        return str(path)

    def write_summary(self, summary: RunSummary) -> str:
        document = RunSummaryDocument(
            run=self._run,
            receipts=summary.receipts,
            completed=summary.completed,
            needs_review=summary.needs_review,
            failed=summary.failed,
            skipped=summary.skipped,
            llm_calls=summary.llm_calls,
            mean_text_attempts=summary.mean_text_attempts,
        )
        path = self._run_dir / "summary.json"
        _write_atomically(path, document.model_dump_json(indent=2))
        return str(path)


def _event(event: TraceEvent) -> TraceEventOut:
    return TraceEventOut(
        seq=event.seq,
        at=event.at,
        agent=event.agent,
        kind=event.kind,
        name=event.name,
        summary=event.summary,
        duration_ms=event.duration_ms,
        data=dict(event.data),
    )


def _tokens(events: list[TraceEvent], key: str) -> int:
    return sum(value for event in events if isinstance(value := event.data.get(key), int))


def _totals(run: ReceiptRun) -> TraceTotals:
    llm = [event for event in run.events if event.kind is TraceKind.LLM]
    tools = [event for event in run.events if event.kind is TraceKind.TOOL]
    return TraceTotals(
        llm_calls=len(llm),
        prompt_tokens=_tokens(llm, "prompt_tokens"),
        completion_tokens=_tokens(llm, "completion_tokens"),
        tool_calls=len(tools),
        tool_denied=sum(1 for event in tools if event.data.get("allowed") is False),
        text_attempts=run.text_attempts,
        vision_attempts=run.vision_attempts,
    )


def _write_atomically(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so that a reader sees the old file or the new one, never
    half of one: a temporary file in the same directory, then a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(text + "\n")
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
