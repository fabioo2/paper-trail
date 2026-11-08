"""In-memory tracer (implements ``paper_trail.ports.trace.Tracer``) with the port's contract:

- ``data`` and ``duration_ms`` are keyword-only;
- ``emit`` outside ``bind`` raises RuntimeError; ``bind`` nests (the innermost receipt wins);
- ``seq`` counts from 1 per receipt and keeps counting across binds (a resumed run appends);
- ``at`` is the clock's time; a summary longer than 90 characters is cut to 89 plus "…";
- card numbers are masked in the summary and in every string inside ``data`` (defence in depth:
  callers pass masked data already).
"""

from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.domain.trace import TraceEvent, TraceKind
from paper_trail.ports.clock import Clock

SUMMARY_LIMIT = 90


def masked_json(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return mask_pans(value).text
    if isinstance(value, list):
        return [masked_json(item) for item in value]
    if isinstance(value, dict):
        return {key: masked_json(item) for key, item in value.items()}
    return value


def short_summary(summary: str) -> str:
    text = mask_pans(summary).text
    return text if len(text) <= SUMMARY_LIMIT else text[: SUMMARY_LIMIT - 1] + "…"


class FakeTracer:
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
            summary=short_summary(summary),
            data={key: masked_json(value) for key, value in (data or {}).items()},
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
