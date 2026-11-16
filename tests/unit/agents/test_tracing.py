"""Every node, Claude read, tool call, route and interrupt is traced (AC-A-09; claude-extraction
plan V11, CE-TC-031).

The runner decorates the reader (``TracingReader``) and the tool gateway and wraps each node, so
nodes hold no tracing code; these tests read the events back from the fake tracer.

Pinned:
- NODE: one event when a node starts (``data["phase"] == "start"``, no duration) and one when
  it ends (``"end"``, ``duration_ms`` >= 0), with the node's agent.
- LLM (one per read, agent EXTRACTION): ``name`` = the prompt tag; ``data`` has ``tag``,
  ``model``, ``attempt`` (1-based per tag), ``input_tokens``, ``output_tokens``,
  ``stop_reason``, ``image_sha256`` (of the bytes sent, never the bytes or base64) and the
  masked ``output``; a failed read has ``error`` (class and message) and no tokens unless it
  was billed. No event holds the image or the API key.
- TOOL: ``name`` = tool; ``data`` has ``role``, ``tool`` and ``allowed``; the event's agent is
  the calling role.
- ROUTE (supervisor): ``name`` = target; ``data`` has ``target``, ``reason`` and ``attempts``.
  INTERRUPT (supervisor): ``name`` "review", ``data`` has ``review_id`` and ``reason``.
- The ``save_receipt`` row (TC-88) appends `` · discounts N linked · M receipt-level`` only
  when the receipt has discount lines; the tool gateway adds one NOTE after ``save_receipt``
  (receipt-level discounts) or ``save_matches`` (unpriced negative items) (TC-91, TC-126).
- ``match`` adds one NOTE ``matching`` (agent MATCHING) per receipt with at least one decision
  (PC-TC-61), its data the count per source.
"""

import base64
import hashlib
import json
from collections.abc import Mapping

import pytest

from paper_trail.agents.tracing import TracingToolGateway, _save_summary, match_counts_summary
from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.domain.trace import TraceEvent, TraceKind
from paper_trail.ports.reader import ReaderError, ReaderUnavailableError
from paper_trail.ports.tools import ToolDescription
from tests.agent_harness import EXTRACT_MODEL, RETRY_MODEL, Harness, build_harness
from tests.extractions import (
    L1,
    L3,
    NOW,
    U1,
    answer,
    coded_answer,
    inconsistent_answer,
    item,
    receipt_answer,
)
from tests.fakes.catalog import TEST_APPLE, FakeCatalog
from tests.fakes.clock import FakeClock
from tests.fakes.images import fake_png
from tests.fakes.reader import END_TURN, INPUT_TOKENS, OUTPUT_TOKENS, ScriptedReader
from tests.fakes.trace import FakeTracer
from tests.unit.agents.graph_support import fresh_label

NODE_AGENTS = {
    "extract": AgentRole.EXTRACTION,
    "validate": AgentRole.VALIDATION,
    "supervisor": AgentRole.SUPERVISOR,
    "review": AgentRole.SUPERVISOR,
    "persist": AgentRole.SUPERVISOR,
    "match": AgentRole.MATCHING,
    "commit_matches": AgentRole.SUPERVISOR,
    "enrich": AgentRole.ENRICHMENT,
    "commit_enrichment": AgentRole.SUPERVISOR,
}
TOOL_ROLES = {
    "check_arithmetic": AgentRole.VALIDATION,
    "save_receipt": AgentRole.SUPERVISOR,
    "open_review": AgentRole.SUPERVISOR,
    "product_lookup": AgentRole.MATCHING,
    "catalog_lookup": AgentRole.MATCHING,
    "save_matches": AgentRole.SUPERVISOR,
    "describe_from_catalog": AgentRole.SUPERVISOR,
}
PHOTO = "traced-photo"
PHOTO_SHA256 = hashlib.sha256(fake_png(PHOTO)).hexdigest()
TEST_KEY = "sk-ant-api03-test-key-never-traced"


def _of(events: tuple[TraceEvent, ...], kind: TraceKind) -> list[TraceEvent]:
    return [event for event in events if event.kind is kind]


def _run(reader: ScriptedReader) -> tuple[TraceEvent, ...]:
    harness = build_harness(reader)
    return harness.events(harness.process_image(PHOTO).receipt_id)


def _retry_then_review() -> tuple[TraceEvent, ...]:
    return _run(ScriptedReader(inconsistent_answer(), then=inconsistent_answer()))


def _clean_run() -> tuple[TraceEvent, ...]:
    return _run(ScriptedReader(answer()))


def test_ac_a_09_events_are_numbered_in_order() -> None:
    """AC-A-09: seq increases by one from 1; every event belongs to the receipt."""
    events = _retry_then_review()

    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert {event.receipt_id for event in events} == {events[0].receipt_id}
    assert all(len(event.summary) <= 90 for event in events)


def test_ac_a_09_nodes_are_bracketed_by_start_and_end() -> None:
    events = _of(_clean_run(), TraceKind.NODE)

    starts = [event for event in events if event.data.get("phase") == "start"]
    ends = [event for event in events if event.data.get("phase") == "end"]
    assert [event.name for event in starts] == [event.name for event in ends]
    assert all(event.duration_ms is None for event in starts)
    assert all(event.duration_ms is not None and event.duration_ms >= 0 for event in ends)
    for event in events:
        assert event.agent is NODE_AGENTS[event.name], event.name


def test_ce_tc_031_each_read_is_an_llm_event_with_model_attempt_tokens_and_image_hash() -> None:
    events = _run(ScriptedReader(inconsistent_answer(), answer()))

    reads = _of(events, TraceKind.LLM)
    assert [
        (
            event.agent,
            event.name == event.data["tag"],
            event.data["model"],
            event.data["attempt"],
            event.data["input_tokens"],
            event.data["output_tokens"],
            event.data["stop_reason"],
            event.data["image_sha256"],
        )
        for event in reads
    ] == [
        (
            AgentRole.EXTRACTION,
            True,
            model,
            attempt,
            INPUT_TOKENS,
            OUTPUT_TOKENS,
            END_TURN,
            PHOTO_SHA256,
        )
        for attempt, model in ((1, EXTRACT_MODEL), (2, RETRY_MODEL))
    ]
    assert all(event.duration_ms is not None and event.duration_ms >= 0 for event in reads)
    output = reads[1].data["output"]
    assert isinstance(output, dict)
    assert output["total"] == "26.83"


def test_ce_tc_031_a_failed_read_records_the_error_and_no_tokens() -> None:
    harness = build_harness(ScriptedReader(ReaderUnavailableError("the Claude API answered 500")))

    with pytest.raises(ReaderError):
        harness.process_image(PHOTO)

    (read,) = _of(harness.events(1), TraceKind.LLM)
    assert (
        read.data["error"],
        read.data["input_tokens"],
        read.data["output_tokens"],
        read.data["image_sha256"],
    ) == ("ReaderUnavailableError: the Claude API answered 500", None, None, PHOTO_SHA256)


def test_ce_tc_031_no_event_holds_the_image_or_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", TEST_KEY)
    image = fake_png(PHOTO)
    forbidden = [
        TEST_KEY,
        base64.b64encode(image).decode(),
        image.decode("latin-1"),
        PHOTO,
    ]
    harness: Harness = build_harness(ScriptedReader(inconsistent_answer(), answer()))

    events = harness.events(harness.process_image(PHOTO).receipt_id)

    dumped = json.dumps([[e.summary, e.data] for e in events], default=str)
    assert [text for text in forbidden if text in dumped] == []


def test_ac_a_09_tool_events_carry_role_tool_and_allowed() -> None:
    """AC-A-09: every TOOL event carries role, tool, allowed; its agent is the role."""
    tool_events = _of(_retry_then_review(), TraceKind.TOOL)

    assert tool_events
    assert tool_events[-1].name == "open_review"
    for event in tool_events:
        role = TOOL_ROLES[event.name]
        assert (event.agent, event.data["role"], event.data["tool"], event.data["allowed"]) == (
            role,
            role.value,
            event.name,
            True,
        )


def test_ac_a_09_routes_and_the_interrupt_are_supervisor_events() -> None:
    events = _retry_then_review()

    routes = _of(events, TraceKind.ROUTE)
    assert [
        (event.name, event.data["target"], event.data["attempts"], event.data["reason"])
        for event in routes
    ] == [("extract", "extract", 1, None), ("review", "review", 2, "validation_failed")]
    (interrupt,) = _of(events, TraceKind.INTERRUPT)
    assert (interrupt.name, interrupt.data["reason"]) == ("review", "validation_failed")
    assert isinstance(interrupt.data["review_id"], int)
    assert all(event.agent is AgentRole.SUPERVISOR for event in [*routes, interrupt])


def test_ac_a_09_kinds_appear_in_causal_order() -> None:
    """The interrupt is last; each read follows its node's start; each route follows a read."""
    kinds = [event.kind for event in _retry_then_review()]

    assert kinds[-1] is TraceKind.INTERRUPT
    assert kinds.index(TraceKind.NODE) < kinds.index(TraceKind.LLM) < kinds.index(TraceKind.ROUTE)


def _saved(items: int, linked: int, receipt_level: int) -> dict[str, JsonValue]:
    """A ``save_receipt`` result as the trace sees it (JSON)."""
    refused: list[JsonValue] = [
        {"position": position, "raw_text": "MULTIPLE", "reason": "multiple"}
        for position in range(receipt_level)
    ]
    return {
        "receipt_id": 1,
        "status": "processing",
        "stage": "matching",
        "item_count": items,
        "masked_pans": 0,
        "discounts_linked": linked,
        "discounts_receipt_level": refused,
    }


# P11 SAVE_SUMMARIES: (items, linked, receipt level) -> the row's summary.
SAVE_SUMMARIES = [
    (22, 7, 1, "PROCESSING · 22 items · discounts 7 linked · 1 receipt-level"),
    (5, 0, 1, "PROCESSING · 5 items · discounts 0 linked · 1 receipt-level"),
    (5, 1, 0, "PROCESSING · 5 items · discounts 1 linked · 0 receipt-level"),
    (4, 0, 0, "PROCESSING · 4 items"),
]


@pytest.mark.parametrize(("items", "linked", "receipt_level", "summary"), SAVE_SUMMARIES)
def test_tc_88_the_save_row_counts_the_discounts(
    items: int, linked: int, receipt_level: int, summary: str
) -> None:
    assert _save_summary(_saved(items, linked, receipt_level)) == summary


class _Returning:
    """A tool gateway that answers every call with ``result``."""

    def __init__(self, result: dict[str, JsonValue]) -> None:
        self._result = result

    def call(
        self, role: AgentRole, name: str, payload: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        return self._result

    def describe(self, role: AgentRole) -> tuple[ToolDescription, ...]:
        return ()


MULTIPLE: JsonValue = {"position": 4, "raw_text": "MULTIPLE", "reason": "multiple"}
SAVINGS: JsonValue = {"position": 6, "raw_text": "SAVINGS", "reason": "no_reference"}
RETURN: JsonValue = {"position": 2, "raw_text": "RETURN KS SOURDOUGH"}
CREDIT: JsonValue = {"position": 5, "raw_text": "CREDIT"}

TOOL_RESULTS: dict[str, dict[str, JsonValue]] = {
    "save_receipt": _saved(5, 0, 0),
    "save_matches": {"lines": [], "new_products": 0, "prices": 0},
    "open_review": {"review_id": 1},
}

# (tool, result, the NOTE as (name, summary, data), or None for no NOTE).
TOOL_NOTES = [
    pytest.param(
        "save_receipt",
        {"discounts_receipt_level": [MULTIPLE, SAVINGS]},
        (
            "discounts",
            "kept at receipt level: pos 4 `MULTIPLE` (multiple); pos 6 `SAVINGS` (no_reference)",
            {"receipt_level": [MULTIPLE, SAVINGS]},
        ),
        id="receipt-level discounts",
    ),
    pytest.param("save_receipt", {"discounts_receipt_level": []}, None, id="every one linked"),
    pytest.param(
        "save_matches",
        {"unpriced": [RETURN, CREDIT]},
        (
            "prices",
            "no price for a negative item line: pos 2 `RETURN KS SOURDOUGH`; pos 5 `CREDIT`",
            {"unpriced": [RETURN, CREDIT]},
        ),
        id="unpriced lines",
    ),
    pytest.param("save_matches", {"unpriced": []}, None, id="every line priced"),
    pytest.param(
        "open_review",
        {"discounts_receipt_level": [MULTIPLE], "unpriced": [RETURN]},
        None,
        id="another tool",
    ),
]


@pytest.mark.parametrize(("tool", "result", "note"), TOOL_NOTES)
def test_tc_91_126_the_tool_gateway_notes_what_a_save_leaves_out(
    tool: str, result: dict[str, JsonValue], note: tuple[str, str, JsonValue] | None
) -> None:
    tracer = FakeTracer(FakeClock(NOW))
    gateway = TracingToolGateway(_Returning({**TOOL_RESULTS[tool], **result}), tracer)

    with tracer.bind(1):
        gateway.call(AgentRole.SUPERVISOR, tool, {})

    kinds = [event.kind for event in tracer.events(1)]
    notes = [
        (event.agent, event.name, event.summary, event.data)
        for event in tracer.events(1)
        if event.kind is TraceKind.NOTE
    ]
    expected = [] if note is None else [(AgentRole.SUPERVISOR, *note)]
    assert (kinds, notes) == ([TraceKind.TOOL] + [TraceKind.NOTE] * len(expected), expected)


# --- barcode lookup PC-TC-61: one NOTE per receipt with the counts by source --------------------

MATCH_COUNTS = [
    pytest.param(
        {
            "catalog": {"store": 5, "gtin": 3, "plu": 1},
            "alias": 1,
            "new": {"receipt_text": 3, "plu": 1},
        },
        "catalog 9 (store 5, gtin 3, plu 1) · alias 1 · new 4 (printed text 3, plu 1)",
        id="every part",
    ),
    pytest.param(
        {"catalog": {"store": 5, "plu": 1}, "new": {"receipt_text": 3}},
        "catalog 6 (store 5, plu 1) · new 3 (printed text 3)",
        id="zero parts left out",
    ),
    pytest.param({"memory": 1, "alias": 2}, "memory 1 · alias 2", id="memory and alias"),
]


@pytest.mark.parametrize(("counts", "summary"), MATCH_COUNTS)
def test_pc_tc_61_the_match_counts_read_as_one_line(
    counts: dict[str, JsonValue], summary: str
) -> None:
    assert match_counts_summary(counts) == summary


def _match_notes(events: tuple[TraceEvent, ...]) -> list[tuple[AgentRole, str, JsonValue]]:
    return [
        (event.agent, event.summary, dict(event.data))
        for event in _of(events, TraceKind.NOTE)
        if event.name == "matching"
    ]


def test_pc_tc_61_match_notes_its_counts_once_per_receipt() -> None:
    lines = [L3, L1, U1]
    harness = build_harness(
        ScriptedReader(coded_answer(lines), coded_answer(lines)),
        catalog=FakeCatalog({"3101": TEST_APPLE}),
    )

    first = harness.process_image(fresh_label()).receipt_id
    second = harness.process_image(fresh_label()).receipt_id

    assert _match_notes(harness.events(first)) == [
        (
            AgentRole.MATCHING,
            "new 3 (printed text 2, plu 1)",
            {"new": {"receipt_text": 2, "plu": 1}},
        )
    ]
    assert _match_notes(harness.events(second)) == [
        (
            AgentRole.MATCHING,
            "catalog 2 (gtin 1, plu 1) · alias 1",
            {"catalog": {"gtin": 1, "plu": 1}, "alias": 1},
        )
    ]
    (save,) = [
        event
        for event in _of(harness.events(second), TraceKind.TOOL)
        if event.name == "save_matches"
    ]
    assert save.summary.startswith("catalog 2 · alias 1")


def _generic_only() -> dict[str, object]:
    """A receipt whose only item is a generic line ("PRODUCE"): nothing to match."""
    return receipt_answer([item("PRODUCE", "3.50")], total="3.50")


def test_pc_tc_61_nothing_to_match_writes_no_note() -> None:
    harness = build_harness(ScriptedReader(_generic_only()))

    receipt_id = harness.process_image(fresh_label()).receipt_id

    assert _match_notes(harness.events(receipt_id)) == []
