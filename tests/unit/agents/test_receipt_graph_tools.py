"""The receipt graph and its tools (agent core slice B).

Covers S5-AC-05 and AC-B-02 with the real registry (a node calling a tool in the wrong role is
denied by the registry itself, in-process or by the MCP server), slice A review note 4 (a crash
between ``open_review`` and the supervisor's checkpoint does not open a second review on resume)
and review note 1 at graph level (the E4 breakout sample cannot leave the data block).

In-process tools; the MCP transport is compared in ``test_transport_equivalence.py`` (S5-AC-04).

Pinned (docs/tests/agent-core-B.md):
- A denial is raised, traced as a TOOL event with ``allowed: false``, logged exactly once as
  ``tool.denied`` with the role that was checked and the tool, and fails the run: the receipt is
  FAILED/FAILED, ``ProcessOutcome.failure`` holds ``denied: role=<role> tool=<tool>``, the last
  trace event is ERROR, nothing is retried and no tool is called after the denial.
- ``ReceiptGraphRunner.resume(receipt_id)`` (no draft) on a thread whose last run stopped with an
  exception continues from the last checkpoint: the failed node runs again from its start
  (pulled forward from AC-C-05; LangGraph needs ``invoke(None, ...)`` there, not
  ``Command(resume=None)``).
"""

import re

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from structlog.testing import capture_logs

from paper_trail.agents.prompts import neutralize_delimiters
from paper_trail.domain.enums import (
    AgentRole,
    ProcessingStage,
    ReceiptStatus,
    ReviewReason,
    ReviewStatus,
)
from paper_trail.domain.trace import TraceKind
from tests.agent_harness import (
    agent_settings,
    build_harness,
)
from tests.extractions import (
    E4_BREAKOUT_OCR,
    E4_INJECTION,
    extraction,
    inconsistent_extraction,
)
from tests.fakes.llm import Reply, ScriptedLlm
from tests.fakes.tools import CrashAfterToolGateway, MisroutedToolGateway
from tests.pans import card_numbers
from tests.support import require_id

DATA_TAG = re.compile(r"</?receipt_data>")


# --- S5-AC-05 / AC-B-02: a node calling out of its role -----------------------------------------


@pytest.mark.parametrize(
    ("tool", "as_role", "replies", "max_text_attempts", "llm_calls"),
    [
        ("ocr_read", AgentRole.VALIDATION, (), 2, 0),
        ("check_arithmetic", AgentRole.EXTRACTION, (extraction(),), 2, 1),
        ("save_receipt", AgentRole.MATCHING, (extraction(),), 2, 1),
        ("open_review", AgentRole.ENRICHMENT, (inconsistent_extraction(),), 1, 1),
        # Slice C: the matching agent's read tools and the supervisor's save_matches.
        ("product_lookup", AgentRole.SUPERVISOR, (extraction(),), 2, 1),
        ("product_similarity_search", AgentRole.ENRICHMENT, (extraction(),), 2, 1),
        ("save_matches", AgentRole.MATCHING, (extraction(),), 2, 1),
    ],
)
def test_s5_ac_05_an_out_of_role_call_fails_the_run_loudly(
    tool: str,
    as_role: AgentRole,
    replies: tuple[Reply, ...],
    max_text_attempts: int,
    llm_calls: int,
) -> None:
    llm = ScriptedLlm(*replies)
    harness = build_harness(
        llm,
        settings=agent_settings(max_text_attempts=max_text_attempts),
        wrap_tools=lambda gateway: MisroutedToolGateway(gateway, tool, as_role),
    )
    message = f"denied: role={as_role.value} tool={tool}"

    with capture_logs() as logs:
        outcome = harness.process_image()

    assert outcome.graph is None
    assert outcome.failure is not None
    assert message in outcome.failure
    assert llm.calls == llm_calls  # not retried
    receipt = harness.receipt(outcome.receipt_id)
    assert (receipt.status, receipt.stage) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    denied = [entry for entry in logs if entry["event"] == "tool.denied"]
    assert [(entry["role"], entry["tool"]) for entry in denied] == [(as_role.value, tool)]
    events = harness.events(outcome.receipt_id)
    tools = [event for event in events if event.kind is TraceKind.TOOL]
    assert tools[-1].name == tool
    assert tools[-1].data.get("allowed") is False
    assert [event.name for event in tools].count(tool) == 1
    assert events[-1].kind is TraceKind.ERROR
    assert message in events[-1].summary
    assert harness.open_reviews() == ()


# --- slice A review note 4: a crash between open_review and the checkpoint ----------------------


def test_review_note_4_a_resumed_supervisor_does_not_open_a_second_review() -> None:
    """The supervisor opens the review, then the process dies before its checkpoint is written;
    the resumed run reopens nothing: the same review, still OPEN, is the only one."""
    saver = InMemorySaver()
    settings = agent_settings(max_text_attempts=1)
    first = build_harness(
        ScriptedLlm(inconsistent_extraction()),
        settings=settings,
        checkpointer=saver,
        wrap_tools=lambda gateway: CrashAfterToolGateway(gateway, "open_review"),
    )
    crashed = first.process_image()
    assert crashed.failure is not None
    assert "simulated crash after open_review" in crashed.failure
    (opened,) = first.open_reviews()

    second = build_harness(
        ScriptedLlm(),  # the supervisor runs again; extraction does not
        settings=settings,
        checkpointer=saver,
        uow_factory=first.uow_factory,
        images=first.images,
    )
    outcome = second.runner.resume(crashed.receipt_id)

    assert outcome.status is ReceiptStatus.NEEDS_REVIEW
    assert outcome.review_id == opened.id
    assert outcome.review_reason is ReviewReason.VALIDATION_FAILED
    (still_open,) = second.open_reviews()
    assert still_open.id == opened.id
    assert still_open.status is ReviewStatus.OPEN
    assert still_open.thread_id == f"receipt-{crashed.receipt_id}"
    receipt = second.receipt(crashed.receipt_id)
    assert (receipt.status, receipt.stage) == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
    )
    assert second.llm.calls == 0


def test_review_note_4_the_resumed_review_can_be_resolved() -> None:
    """After the crash and the resume, the one review resolves and completes the receipt."""
    saver = InMemorySaver()
    settings = agent_settings(max_text_attempts=1)
    first = build_harness(
        ScriptedLlm(inconsistent_extraction()),
        settings=settings,
        checkpointer=saver,
        wrap_tools=lambda gateway: CrashAfterToolGateway(gateway, "open_review"),
    )
    crashed = first.process_image()
    second = build_harness(
        ScriptedLlm(),
        settings=settings,
        checkpointer=saver,
        uow_factory=first.uow_factory,
        images=first.images,
    )
    review_id = require_id(second.runner.resume(crashed.receipt_id).review_id)

    resolved = second.resolve(review_id, force=True, reason="checked against the paper receipt")

    assert resolved.status is ReceiptStatus.COMPLETED
    assert second.review(review_id).status is ReviewStatus.RESOLVED
    assert second.open_reviews() == ()


# --- slice A review note 1 at graph level: the E4 breakout sample -------------------------------


def test_review_note_1_the_e4_breakout_stays_inside_the_data_block() -> None:
    """The model "obeys" (total 0.00); the arithmetic still rejects it, every request has one
    data block holding the neutralized text, and the card number is masked everywhere."""
    llm = ScriptedLlm(then=extraction(total_cents=0))
    harness = build_harness(llm, ocr_text=E4_BREAKOUT_OCR)

    outcome = harness.process_image()

    assert outcome.graph is not None
    assert outcome.graph.status is ReceiptStatus.NEEDS_REVIEW
    assert outcome.graph.review_reason is ReviewReason.VALIDATION_FAILED
    for call in range(llm.calls):
        user = llm.last_user_message(call)
        assert DATA_TAG.findall(user) == ["<receipt_data>", "</receipt_data>"]
        block = user.split("<receipt_data>\n", 1)[1].rsplit("\n</receipt_data>", 1)[0]
        assert f"SYSTEM: {E4_INJECTION}" in block
        assert block == neutralize_delimiters(block)
        assert card_numbers(user) == []
