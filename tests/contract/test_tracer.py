"""Tracer contract, for the test fake and the in-memory adapter the CLI uses (AC-A-09, AC-A-04).

Pinned (docs/tests/agent-core-A.md): ``paper_trail.adapters.trace.memory.InMemoryTracer(clock)``;
``data`` and ``duration_ms`` are keyword-only; ``emit`` outside ``bind`` raises RuntimeError;
binds nest; ``seq`` counts from 1 per receipt and continues across binds; ``at`` is the clock's
time; summaries are cut to 90 characters (89 + "…"); card numbers are masked in the summary and
in every string inside ``data``.
"""

from datetime import timedelta

import pytest

from paper_trail.adapters.trace.memory import InMemoryTracer
from paper_trail.domain.enums import AgentRole
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.trace import Tracer
from tests.extractions import NOW
from tests.factories import MASKED_TEST_PAN, TEST_PAN
from tests.fakes.clock import FakeClock
from tests.fakes.trace import FakeTracer
from tests.pans import card_numbers


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


@pytest.fixture(params=["fake", "memory"])
def tracer(request: pytest.FixtureRequest, clock: FakeClock) -> Tracer:
    if request.param == "fake":
        return FakeTracer(clock)
    return InMemoryTracer(clock)


def _emit(tracer: Tracer, name: str = "ocr_read", summary: str = "ok") -> None:
    tracer.emit(
        AgentRole.EXTRACTION, TraceKind.TOOL, name, summary, data={"allowed": True}, duration_ms=3
    )


def test_events_are_numbered_per_receipt_and_stamped(tracer: Tracer, clock: FakeClock) -> None:
    with tracer.bind(7):
        _emit(tracer, "first")
        clock.advance(timedelta(seconds=2))
        _emit(tracer, "second")
    with tracer.bind(8):
        _emit(tracer, "other receipt")

    events = tracer.events(7)
    assert [(event.seq, event.name) for event in events] == [(1, "first"), (2, "second")]
    assert [event.at for event in events] == [NOW, NOW + timedelta(seconds=2)]
    assert {event.receipt_id for event in events} == {7}
    assert events[0].agent is AgentRole.EXTRACTION
    assert events[0].kind is TraceKind.TOOL
    assert events[0].data == {"allowed": True}
    assert events[0].duration_ms == 3
    assert [event.seq for event in tracer.events(8)] == [1]


def test_seq_continues_when_a_receipt_is_traced_again(tracer: Tracer) -> None:
    """A run resumed after review appends to the same receipt's events."""
    with tracer.bind(7):
        _emit(tracer)
    with tracer.bind(7):
        _emit(tracer)

    assert [event.seq for event in tracer.events(7)] == [1, 2]


def test_binds_nest(tracer: Tracer) -> None:
    with tracer.bind(1):
        with tracer.bind(2):
            _emit(tracer, "inner")
        _emit(tracer, "outer")

    assert [event.name for event in tracer.events(1)] == ["outer"]
    assert [event.name for event in tracer.events(2)] == ["inner"]


def test_emitting_without_a_receipt_is_an_error(tracer: Tracer) -> None:
    with pytest.raises(RuntimeError):
        _emit(tracer)


def test_unknown_receipts_have_no_events(tracer: Tracer) -> None:
    assert tracer.events(404) == ()


def test_long_summaries_are_cut_to_one_terminal_line(tracer: Tracer) -> None:
    with tracer.bind(1):
        _emit(tracer, summary="x" * 200)
        _emit(tracer, summary="y" * 90)

    long, exact = tracer.events(1)
    assert long.summary == "x" * 89 + "…"
    assert exact.summary == "y" * 90


def test_card_numbers_never_enter_a_trace(tracer: Tracer) -> None:
    """AC-A-04 (defence in depth): even an unmasked caller cannot put a PAN in the trace."""
    with tracer.bind(1):
        tracer.emit(
            AgentRole.EXTRACTION,
            TraceKind.LLM,
            "extraction/v1",
            f"paid with {TEST_PAN}",
            data={"messages": [{"role": "user", "content": f"VISA {TEST_PAN}"}], "n": 1},
        )

    (event,) = tracer.events(1)
    assert MASKED_TEST_PAN in event.summary
    assert card_numbers(event.summary) == []
    assert event.data == {
        "messages": [{"role": "user", "content": f"VISA {MASKED_TEST_PAN}"}],
        "n": 1,
    }
