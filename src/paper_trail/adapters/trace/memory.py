"""The tracer of a CLI process: events kept in memory, per receipt (AC-A-09).

Summaries are cut to one terminal line and card numbers are masked in the summary and in every
string of the data, as defence in depth: callers pass masked data already (ADR-0011).
"""

from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.domain.rules.masking import mask_json, mask_pans
from paper_trail.domain.trace import TraceEvent, TraceKind
from paper_trail.ports.clock import Clock

SUMMARY_LIMIT = 90


class InMemoryTracer:
    """Implements ``paper_trail.ports.trace.Tracer``."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._events: defaultdict[int, list[TraceEvent]] = defaultdict(list)
        self._bound: list[int] = []

    def emit(  # noqa: PLR0913 - one argument per caller-supplied TraceEvent field
        self,
        agent: AgentRole,
        kind: TraceKind,
        name: str,
        summary: str,
        *,
        data: Mapping[str, JsonValue] | None = None,
        duration_ms: int | None = None,
    ) -> TraceEvent:
        if not self._bound:
            msg = "emit() outside bind(): no receipt to trace"
            raise RuntimeError(msg)
        receipt_id = self._bound[-1]
        events = self._events[receipt_id]
        event = TraceEvent(
            seq=len(events) + 1,
            at=self._clock.now(),
            receipt_id=receipt_id,
            agent=agent,
            kind=kind,
            name=name,
            summary=_one_line(summary),
            data={key: mask_json(value) for key, value in (data or {}).items()},
            duration_ms=duration_ms,
        )
        events.append(event)
        return event

    def events(self, receipt_id: int) -> tuple[TraceEvent, ...]:
        return tuple(self._events.get(receipt_id, ()))

    @contextmanager
    def bind(self, receipt_id: int) -> Iterator[None]:
        self._bound.append(receipt_id)
        try:
            yield
        finally:
            self._bound.pop()


def _one_line(summary: str) -> str:
    text = mask_pans(" ".join(summary.split())).text
    return text if len(text) <= SUMMARY_LIMIT else text[: SUMMARY_LIMIT - 1] + "…"
