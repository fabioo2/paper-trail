"""A retried read timeout shows on the call's LLM trace row (reliability plan, answer Q4).

Pinned: ``TracingLlm`` keeps one LLM event per call. When the call's ``LlmResult.timeout_retries``
(or the failed call's ``LlmError.timeout_retries``) is above 0, the event's data carries it as
``timeout_retries`` and its summary starts with ``↻N `` so the trace table's 42-character cell
keeps it; with 0 the row is as before. The error text is never changed.
"""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel, TypeAdapter

from paper_trail.adapters.ollama.llm import OllamaLlm
from paper_trail.agents.tracing import TracingLlm
from paper_trail.domain.trace import TraceEvent
from paper_trail.interfaces.cli.trace_view import rows, styler
from paper_trail.ports.llm import ChatMessage, LlmPort, LlmRequest, LlmResult, LlmUnavailableError
from tests.extractions import NOW
from tests.fakes.clock import FakeClock
from tests.fakes.trace import FakeTracer
from tests.ollama_wire import BASE_URL, ollama_settings

RECEIPT_ID = 7
REQUEST = LlmRequest(
    model="text-model:test",
    tag="extraction/v1",
    messages=(ChatMessage(role="user", content="TOTAL 26.83"),),
)
SMALL = {"total_cents": 2683}
EXTRACTION = {"items": [{"raw_text": "EGGS"}] * 12, "total_cents": 2683, "confidence": 0.92}


class Answer(BaseModel):
    total_cents: int


class Extraction(BaseModel):
    items: list[dict[str, str]]
    total_cents: int
    confidence: float


class AnsweringLlm:
    """An ``LlmPort`` whose one answer is ``payload`` and reports ``retries`` retries."""

    def __init__(self, payload: dict[str, Any], retries: int) -> None:
        self._payload = payload
        self._retries = retries

    def complete_structured[M](self, request: LlmRequest, schema: type[M]) -> LlmResult[M]:
        del request
        value = TypeAdapter(schema).validate_python(self._payload)
        return LlmResult(
            value=value, prompt_tokens=11, completion_tokens=7, timeout_retries=self._retries
        )


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


def _traced_call(llm: LlmPort, schema: type[BaseModel] = Answer) -> TraceEvent:
    tracer = FakeTracer(FakeClock(NOW))
    with tracer.bind(RECEIPT_ID):
        TracingLlm(llm, tracer, RECEIPT_ID).complete_structured(REQUEST, schema)
    (event,) = tracer.events(RECEIPT_ID)
    return event


@pytest.mark.parametrize(
    ("retries", "summary", "count"), [(0, "ok", None), (2, "↻2 ok", 2)], ids=["none", "two"]
)
def test_rl_q4_a_successful_call_records_its_retries(
    retries: int, summary: str, count: int | None
) -> None:
    event = _traced_call(AnsweringLlm(SMALL, retries))

    assert (event.summary, event.data.get("timeout_retries")) == (summary, count)


def test_rl_q4_a_call_that_fails_after_a_retry_records_them_with_the_error_unchanged(
    router: respx.MockRouter,
) -> None:
    router.post("/api/chat").mock(
        side_effect=[
            httpx.ReadTimeout("read timeout"),
            httpx.Response(500, json={"error": "model failed to load"}),
        ]
    )
    error = "the LLM host llm-host.test:11434 answered 500: model failed to load"
    tracer = FakeTracer(FakeClock(NOW))
    llm = TracingLlm(OllamaLlm(ollama_settings(timeout_retries=1)), tracer, RECEIPT_ID)

    with tracer.bind(RECEIPT_ID), pytest.raises(LlmUnavailableError):
        llm.complete_structured(REQUEST, Answer)

    (event,) = tracer.events(RECEIPT_ID)
    assert (event.summary[:27], event.data["timeout_retries"], event.data["error"]) == (
        "↻1 LlmUnavailableError: the",
        1,
        error,
    )


def test_rl_q4_the_trace_table_row_shows_the_retries() -> None:
    """A real extraction summary is longer than the result cell; the marker leads, so it stays."""
    event = _traced_call(AnsweringLlm(EXTRACTION, 1), Extraction)

    (_header, row) = rows([event], styler(color=False), verbose=False)

    assert "↻1 12 lines · total 26.83" in row
