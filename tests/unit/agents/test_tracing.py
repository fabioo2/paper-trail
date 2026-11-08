"""Every node, LLM call, tool call, route and interrupt is traced (AC-A-09).

The runner decorates the LLM port and the tool gateway (``agents/tracing.py``) and wraps each
node, so nodes hold no tracing code; these tests read the events back from the fake tracer.

Pinned (docs/tests/agent-core-A.md):
- NODE: one event when a node starts (``data["phase"] == "start"``, no duration) and one when
  it ends (``"end"``, ``duration_ms`` >= 0), with the node's agent: read and extract_text ->
  extraction, validate -> validation, supervisor, review and persist -> supervisor.
- LLM: ``name`` = prompt tag; ``data`` has ``tag``, ``model``, ``attempt`` (1-based count of this
  tag's calls for the receipt), ``prompt_tokens``, ``completion_tokens``, ``messages`` (role and
  content of each) and ``output`` (the validated output as JSON); ``duration_ms`` is set.
- TOOL: ``name`` = tool; ``data`` has ``role``, ``tool`` and ``allowed``; the event's agent is
  the calling role.
- ROUTE (supervisor): ``name`` = target; ``data`` has ``target``, ``reason``, ``text_attempts``
  and ``vision_attempts``. INTERRUPT (supervisor): ``name`` "review", ``data`` has
  ``review_id`` and ``reason``.
"""

from paper_trail.domain.enums import AgentRole
from paper_trail.domain.trace import TraceEvent, TraceKind
from tests.agent_harness import TEXT_MODEL, build_harness
from tests.extractions import extraction, inconsistent_extraction
from tests.fakes.llm import COMPLETION_TOKENS, PROMPT_TOKENS, ScriptedLlm

NODE_AGENTS = {
    "read": AgentRole.EXTRACTION,
    "extract_text": AgentRole.EXTRACTION,
    "extract_vision": AgentRole.EXTRACTION,  # slice D
    "validate": AgentRole.VALIDATION,
    "supervisor": AgentRole.SUPERVISOR,
    "review": AgentRole.SUPERVISOR,
    "persist": AgentRole.SUPERVISOR,
    "match": AgentRole.MATCHING,  # slice C
    "commit_matches": AgentRole.SUPERVISOR,
    "enrich": AgentRole.ENRICHMENT,  # slice D
    "commit_enrichment": AgentRole.SUPERVISOR,
}
TOOL_ROLES = {
    "ocr_read": AgentRole.EXTRACTION,
    "check_arithmetic": AgentRole.VALIDATION,
    "save_receipt": AgentRole.SUPERVISOR,
    "open_review": AgentRole.SUPERVISOR,
    "product_lookup": AgentRole.MATCHING,  # slice C
    "product_similarity_search": AgentRole.MATCHING,
    "save_matches": AgentRole.SUPERVISOR,
    "save_enrichment": AgentRole.SUPERVISOR,  # slice D
}


def _of(events: tuple[TraceEvent, ...], kind: TraceKind) -> list[TraceEvent]:
    return [event for event in events if event.kind is kind]


def _retry_then_review() -> tuple[TraceEvent, ...]:
    harness = build_harness(ScriptedLlm(inconsistent_extraction(), then=inconsistent_extraction()))
    return harness.events(harness.process_image().receipt_id)


def _clean_run() -> tuple[TraceEvent, ...]:
    harness = build_harness(ScriptedLlm(extraction()))
    return harness.events(harness.process_image().receipt_id)


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


def test_ac_a_09_llm_events_carry_tag_model_attempt_tokens_and_messages() -> None:
    """AC-A-09: every LLM event carries tag, model, attempt and duration (and the masked
    rendered messages and output, for the JSON trace)."""
    llm_events = _of(_retry_then_review(), TraceKind.LLM)

    assert [event.data["attempt"] for event in llm_events] == [1, 2]
    for event in llm_events:
        assert event.agent is AgentRole.EXTRACTION
        assert event.name == "extraction/v1"
        assert event.data["tag"] == "extraction/v1"
        assert event.data["model"] == TEXT_MODEL
        assert event.data["prompt_tokens"] == PROMPT_TOKENS
        assert event.data["completion_tokens"] == COMPLETION_TOKENS
        assert event.duration_ms is not None
        assert event.duration_ms >= 0
        messages = event.data["messages"]
        assert isinstance(messages, list)
        first = messages[0]
        assert isinstance(first, dict)
        assert first["role"] == "system"
        assert isinstance(first["content"], str)
        output = event.data["output"]
        assert isinstance(output, dict)
        assert output["total_cents"] == 2683


def test_ac_a_09_tool_events_carry_role_tool_and_allowed() -> None:
    """AC-A-09: every TOOL event carries role, tool, allowed; its agent is the role."""
    tool_events = _of(_retry_then_review(), TraceKind.TOOL)

    assert [event.name for event in tool_events] == [
        "ocr_read",
        "check_arithmetic",
        "check_arithmetic",
        "open_review",
    ]
    for event in tool_events:
        role = TOOL_ROLES[event.name]
        assert event.agent is role
        assert event.data["role"] == role.value
        assert event.data["tool"] == event.name
        assert event.data["allowed"] is True


def test_ac_a_09_routes_and_the_interrupt_are_supervisor_events() -> None:
    events = _retry_then_review()

    routes = _of(events, TraceKind.ROUTE)
    assert [event.name for event in routes] == ["extract_text", "review"]
    assert [event.data["target"] for event in routes] == ["extract_text", "review"]
    assert [event.data["text_attempts"] for event in routes] == [1, 2]
    assert [event.data["reason"] for event in routes] == [None, "validation_failed"]
    assert all(event.data["vision_attempts"] == 0 for event in routes)
    (interrupt,) = _of(events, TraceKind.INTERRUPT)
    assert interrupt.name == "review"
    assert interrupt.data["reason"] == "validation_failed"
    assert isinstance(interrupt.data["review_id"], int)
    assert all(event.agent is AgentRole.SUPERVISOR for event in [*routes, interrupt])


def test_ac_a_09_kinds_appear_in_causal_order() -> None:
    """AC-A-09: NODE/TOOL/LLM/ROUTE/INTERRUPT in order: the interrupt is last, each LLM call
    follows its node's start, each route follows a validation."""
    events = _retry_then_review()
    kinds = [event.kind for event in events]

    assert kinds[-1] is TraceKind.INTERRUPT
    assert kinds.index(TraceKind.TOOL) < kinds.index(TraceKind.LLM) < kinds.index(TraceKind.ROUTE)


def _ocr_event(rotation_degrees: int) -> TraceEvent:
    harness = build_harness(ScriptedLlm(extraction()))
    harness.ocr.rotation_degrees = rotation_degrees
    events = harness.events(harness.process_image().receipt_id)
    (event,) = [event for event in _of(events, TraceKind.TOOL) if event.name == "ocr_read"]
    return event


def test_the_ocr_event_shows_the_chosen_rotation() -> None:
    """docs/tests/ocr-rotation.md: the trace says the photo was read turned 90 degrees."""
    event = _ocr_event(90)

    output = event.data["output"]
    assert isinstance(output, dict)
    assert output["rotation_degrees"] == 90
    assert "rotated 90°" in event.summary
    assert len(event.summary) <= 90


def test_an_upright_ocr_event_does_not_mention_rotation() -> None:
    event = _ocr_event(0)

    output = event.data["output"]
    assert isinstance(output, dict)
    assert output["rotation_degrees"] == 0
    assert "rotated" not in event.summary
