"""The receipt graph end to end over fakes: extract, validate, retry with feedback, review, save.

Covers S3-AC-02, S3-AC-03, S3-AC-05, S3-AC-09, S3-AC-11 (graph level), AC-A-05, AC-A-06,
AC-A-07, AC-A-11 and the failure paths of AC-A-10 (LLM host down, run failed). Everything goes
through ``ProcessReceiptFile`` with the scripted LLM, the in-process tool gateway and fakes.

Pinned (docs/tests/agent-core-A.md):
- every extraction request uses ``settings.ollama.text_model``, temperature 0 and the prompt tag;
- ``GraphOutcome.paused_at`` is None for a review (a review is not a ``--interrupt-after`` pause);
- the review item's ``thread_id`` is ``receipt-<id>``; its ``details["issues"]`` has the Stage 1
  shape (``code``, ``message``, ``expected``, ``actual``, ``position``) for the LAST attempt;
  ``[]`` for EXTRACTION_FAILED;
- vision is off in these tests (``agent_settings`` defaults to ``max_vision_attempts=0``); the
  vision fallback is ``test_vision_fallback.py`` (slice D);
- a run that raises anything but ``LlmUnavailableError`` leaves the receipt FAILED/FAILED, emits
  an ERROR trace event and returns ``ProcessOutcome(graph=None, failure=<message>)``;
  ``LlmUnavailableError`` propagates and leaves the receipt PROCESSING (checkpoint kept).

In-process tools; the MCP transport is compared in ``test_transport_equivalence.py`` (S5-AC-04).
"""

import re

import pytest

from paper_trail.application.process_receipt import ProcessOutcome
from paper_trail.domain.enums import (
    AgentRole,
    ProcessingStage,
    ReceiptStatus,
    ReviewReason,
    ReviewStatus,
)
from paper_trail.domain.models import JsonValue
from paper_trail.domain.money import Money
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.agents import GraphOutcome
from paper_trail.ports.llm import LlmUnavailableError
from tests.agent_harness import (
    TEXT_MODEL,
    Harness,
    agent_settings,
    build_harness,
)
from tests.extractions import (
    COSTCO_OCR,
    GENERIC_RETRY_LINE,
    INJECTED_OCR,
    INJECTION,
    LINE1_ISSUE,
    LINE3_ISSUE,
    MASKED_PAN,
    PURCHASED_ON,
    STORE_NAME,
    SUBTOTAL_ISSUE,
    TEST_PAN_DIGITS,
    extraction,
    inconsistent_extraction,
)
from tests.factories import TEST_PAN
from tests.fakes.llm import ScriptedLlm
from tests.fakes.tools import DenyingToolGateway
from tests.pans import card_numbers
from tests.support import require_id

LLM = TraceKind.LLM
TOOL = TraceKind.TOOL
ROUTE = TraceKind.ROUTE
INTERRUPT = TraceKind.INTERRUPT
# Slice C: the three new lines on a fresh database, each looked up then searched, then committed.
MATCHING_STEPS = [
    *[(TOOL, "product_lookup"), (TOOL, "product_similarity_search")] * 3,
    (TOOL, "save_matches"),
]


def _graph(outcome: ProcessOutcome) -> GraphOutcome:
    assert outcome.failure is None, outcome.failure
    assert outcome.graph is not None
    return outcome.graph


def _outside_data_blocks(text: str) -> str:
    return re.sub(r"<receipt_data>.*?</receipt_data>", "", text, flags=re.DOTALL)


def _review_issues(harness: Harness, review_id: int | None) -> list[dict[str, JsonValue]]:
    issues = harness.review(require_id(review_id)).details["issues"]
    assert isinstance(issues, list)
    return [issue for issue in issues if isinstance(issue, dict)]


def _review_messages(harness: Harness, review_id: int | None) -> list[JsonValue]:
    return [issue["message"] for issue in _review_issues(harness, review_id)]


# --- S3-AC-02: a clean receipt ------------------------------------------------------------------


def test_s3_ac_02_consistent_extraction_is_validated_and_saved() -> None:
    """S3-AC-02: extract -> validate -> persist; COMPLETED with the extracted items."""
    harness = build_harness(ScriptedLlm(extraction()))

    outcome = harness.process_image()

    graph = _graph(outcome)
    assert outcome.duplicate is False
    assert graph == GraphOutcome(
        receipt_id=outcome.receipt_id,
        status=ReceiptStatus.COMPLETED,
        review_id=None,
        review_reason=None,
        text_attempts=1,
        vision_attempts=0,
        paused_at=None,
    )
    receipt = harness.receipt(outcome.receipt_id)
    assert (receipt.status, receipt.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert receipt.store_name == STORE_NAME
    assert receipt.store_key == "costco"
    assert receipt.purchased_on == PURCHASED_ON  # "11/01/2026" from the model, normalized
    assert receipt.total == Money(2683, "CAD")
    assert [item.raw_text for item in receipt.items] == [
        "KS ORG EGGS 24CT",
        "KS 2% MILK 4L",
        "KS SOURDOUGH",
    ]
    assert receipt.image_path is not None
    assert receipt.image_sha256 is not None
    assert harness.open_reviews() == ()


def test_s3_ac_02_runs_the_nodes_in_order() -> None:
    """S3-AC-02 (agent core: progress is the trace, Stage 2 maps it to receipt events)."""
    harness = build_harness(ScriptedLlm(extraction()))

    receipt_id = harness.process_image().receipt_id

    assert harness.node_runs(receipt_id) == [
        "read",
        "extract_text",
        "validate",
        "supervisor",
        "persist",
        "match",  # slice C
        "commit_matches",
    ]
    assert harness.steps(receipt_id) == [
        (TOOL, "ocr_read"),
        (LLM, "extraction/v1"),
        (TOOL, "check_arithmetic"),
        (ROUTE, "persist"),
        (TOOL, "save_receipt"),
        *MATCHING_STEPS,
    ]


def test_s3_ac_02_extraction_request_shape() -> None:
    """The request: the text model, temperature 0, the prompt tag, no images, masked text."""
    llm = ScriptedLlm(extraction())
    harness = build_harness(llm)

    harness.process_image()

    (request,) = llm.requests
    assert request.model == TEXT_MODEL
    assert request.temperature == 0.0
    assert request.tag == "extraction/v1"
    assert all(message.images == () for message in request.messages)
    final = llm.last_user_message(0)
    assert MASKED_PAN in final
    assert TEST_PAN not in final


def test_ac_a_13_no_examples_setting_reaches_the_request() -> None:
    """AC-A-13 / E1 optional arm: PROMPTS__EXAMPLES=false sends no assistant messages."""
    llm = ScriptedLlm(extraction())
    harness = build_harness(llm, settings=agent_settings(examples=False))

    harness.process_image()

    assert [message.role for message in llm.requests[0].messages] == ["system", "user"]
    assert llm.requests[0].tag == "extraction/v1+noex"


# --- S3-AC-03 and AC-A-06: the validation feedback loop ------------------------------------------


def test_s3_ac_03_second_attempt_carries_the_specific_issues() -> None:
    """S3-AC-03: the retry contains the validation messages verbatim; COMPLETED after 2."""
    llm = ScriptedLlm(inconsistent_extraction(), extraction())
    harness = build_harness(llm)

    outcome = harness.process_image()

    graph = _graph(outcome)
    assert (graph.status, graph.text_attempts) == (ReceiptStatus.COMPLETED, 2)
    assert llm.calls == 2
    first, second = llm.last_user_message(0), llm.last_user_message(1)
    assert "<validation_feedback>" not in first
    assert LINE3_ISSUE in second
    assert SUBTOTAL_ISSUE in second
    assert LINE3_ISSUE in _outside_data_blocks(second)  # feedback is not receipt data
    assert harness.steps(outcome.receipt_id) == [
        (TOOL, "ocr_read"),
        (LLM, "extraction/v1"),
        (TOOL, "check_arithmetic"),
        (ROUTE, "extract_text"),
        (LLM, "extraction/v1"),
        (TOOL, "check_arithmetic"),
        (ROUTE, "persist"),
        (TOOL, "save_receipt"),
        *MATCHING_STEPS,
    ]
    assert harness.receipt(outcome.receipt_id).items[2].line_total == Money(998, "CAD")


def test_ac_a_06_without_feedback_the_retry_gets_the_generic_line_only() -> None:
    """AC-A-06 (E1 control arm): the generic retry line and none of the issue messages."""
    llm = ScriptedLlm(inconsistent_extraction(), extraction())
    harness = build_harness(llm, settings=agent_settings(feedback=False))

    graph = _graph(harness.process_image())

    assert graph.text_attempts == 2
    second = "\n".join(llm.contents(1))
    assert GENERIC_RETRY_LINE in second
    assert LINE3_ISSUE not in second
    assert SUBTOTAL_ISSUE not in second


def test_ac_a_06_a_single_attempt_goes_straight_to_review() -> None:
    """AC-A-06 (E1 single-attempt arm): max_text_attempts=1 -> VALIDATION_FAILED review."""
    llm = ScriptedLlm(inconsistent_extraction())
    harness = build_harness(llm, settings=agent_settings(max_text_attempts=1))

    graph = _graph(harness.process_image())

    assert llm.calls == 1
    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    assert graph.review_reason is ReviewReason.VALIDATION_FAILED
    assert graph.text_attempts == 1


# --- AC-A-07: always inconsistent ---------------------------------------------------------------


def test_ac_a_07_always_inconsistent_parks_the_receipt_for_review() -> None:
    """AC-A-07 / S3-AC-04 minus vision: two text attempts, then interrupt with an OPEN
    VALIDATION_FAILED review holding the LAST attempt's issues."""
    llm = ScriptedLlm(inconsistent_extraction(), then=inconsistent_extraction(line=1))
    harness = build_harness(llm, settings=agent_settings(max_vision_attempts=0))

    outcome = harness.process_image()

    graph = _graph(outcome)
    assert llm.calls == 2
    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    assert graph.review_reason is ReviewReason.VALIDATION_FAILED
    assert (graph.text_attempts, graph.vision_attempts, graph.paused_at) == (2, 0, None)
    review = harness.review(require_id(graph.review_id))
    assert review.status is ReviewStatus.OPEN
    assert review.reason is ReviewReason.VALIDATION_FAILED
    assert review.receipt_id == outcome.receipt_id
    assert review.thread_id == f"receipt-{outcome.receipt_id}"
    assert _review_messages(harness, graph.review_id) == [LINE1_ISSUE, SUBTOTAL_ISSUE]
    for issue in _review_issues(harness, graph.review_id):
        assert set(issue) == {"code", "message", "expected", "actual", "position"}
    receipt = harness.receipt(outcome.receipt_id)
    assert (receipt.status, receipt.stage) == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
    )
    assert harness.steps(outcome.receipt_id)[-3:] == [
        (ROUTE, "review"),
        (TOOL, "open_review"),
        (INTERRUPT, "review"),
    ]


# --- S3-AC-05 and S3-AC-11: confidence and unparseable output -----------------------------------


def test_s3_ac_05_low_confidence_goes_to_review_without_retrying() -> None:
    """S3-AC-05: a consistent extraction below MIN_CONFIDENCE -> LOW_CONFIDENCE review."""
    llm = ScriptedLlm(extraction(confidence=0.3))
    harness = build_harness(llm)

    graph = _graph(harness.process_image())

    assert llm.calls == 1
    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    assert graph.review_reason is ReviewReason.LOW_CONFIDENCE
    assert harness.review(require_id(graph.review_id)).reason is ReviewReason.LOW_CONFIDENCE


def test_s3_ac_05_confidence_at_the_threshold_is_enough() -> None:
    harness = build_harness(ScriptedLlm(extraction(confidence=0.6)))

    assert _graph(harness.process_image()).status is ReceiptStatus.COMPLETED


def test_s3_ac_11_output_that_never_validates_ends_in_extraction_failed() -> None:
    """S3-AC-11: invalid output (extra field, then not JSON) is never saved; it is retried
    like an inconsistent one and then reviewed as EXTRACTION_FAILED."""
    llm = ScriptedLlm(extraction(notes="extra field"), "this is not JSON")
    harness = build_harness(llm)

    graph = _graph(harness.process_image())

    assert llm.calls == 2
    assert graph.review_reason is ReviewReason.EXTRACTION_FAILED
    assert harness.review(require_id(graph.review_id)).details["issues"] == []
    assert harness.receipt(graph.receipt_id).items == ()


def test_s3_ac_11_unparsed_then_valid_completes() -> None:
    llm = ScriptedLlm(extraction(total_cents="2683"), extraction())
    harness = build_harness(llm)

    graph = _graph(harness.process_image())

    assert (graph.status, graph.text_attempts) == (ReceiptStatus.COMPLETED, 2)


# --- S3-AC-09: prompt injection ------------------------------------------------------------------


def test_s3_ac_09_injected_text_stays_inside_the_data_block() -> None:
    """S3-AC-09: the injected line reaches the model only inside <receipt_data>, and a
    manipulated total is still rejected by the code-checked arithmetic."""
    llm = ScriptedLlm(then=extraction(total_cents=0))  # the model "obeyed" the injection
    harness = build_harness(llm, ocr_text=INJECTED_OCR)

    graph = _graph(harness.process_image())

    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    assert graph.review_reason is ReviewReason.VALIDATION_FAILED
    assert INJECTION in llm.last_user_message(0)
    for request in llm.requests:
        for message in request.messages:
            assert INJECTION not in _outside_data_blocks(message.content), message.role
    messages = _review_messages(harness, graph.review_id)
    assert any("total is 0.00" in str(message) for message in messages), messages


# --- AC-A-11: text input -------------------------------------------------------------------------


def test_ac_a_11_text_input_is_masked_first_and_skips_read() -> None:
    """AC-A-11: --text-file masks before anything else, skips `read` and never runs OCR."""
    llm = ScriptedLlm(extraction())
    harness = build_harness(llm)

    outcome = harness.process_text(COSTCO_OCR, name="costco.txt")

    graph = _graph(outcome)
    assert graph.status is ReceiptStatus.COMPLETED
    assert harness.ocr.images == []
    assert harness.node_runs(outcome.receipt_id)[0] == "extract_text"
    assert (TOOL, "ocr_read") not in harness.steps(outcome.receipt_id)
    final = llm.last_user_message(0)
    assert MASKED_PAN in final
    assert card_numbers(final) == []
    receipt = harness.receipt(outcome.receipt_id)
    assert receipt.image_path is None
    assert receipt.image_sha256 is None
    assert receipt.ocr_text is not None
    assert MASKED_PAN in receipt.ocr_text
    assert TEST_PAN_DIGITS not in receipt.ocr_text.replace(" ", "")


def test_ac_a_11_text_inputs_are_never_deduplicated() -> None:
    harness = build_harness(ScriptedLlm(then=extraction()))

    first = harness.process_text(COSTCO_OCR)
    second = harness.process_text(COSTCO_OCR)

    assert first.receipt_id != second.receipt_id
    assert second.duplicate is False


# --- AC-A-04 (unit level; the SQLite and checkpoint scan is in tests/integration) ---------------


def test_ac_a_04_no_card_number_reaches_requests_trace_reviews_or_storage() -> None:
    """AC-A-04: OCR returns a full card number; ocr_read masks it, so no LLM request, trace
    event, review detail or stored field holds a Luhn-valid 13-19 digit run."""
    llm = ScriptedLlm(inconsistent_extraction(), extraction())
    harness = build_harness(llm)

    outcome = harness.process_image()

    for request in llm.requests:
        for message in request.messages:
            assert card_numbers(message.content) == [], message.role
    for event in harness.events(outcome.receipt_id):
        assert card_numbers(event.summary) == []
        assert card_numbers(repr(event.data)) == [], event.name
    receipt = harness.receipt(outcome.receipt_id)
    assert receipt.ocr_text is not None
    assert card_numbers(repr(receipt)) == []
    ocr_events = [event for event in harness.events(outcome.receipt_id) if event.name == "ocr_read"]
    assert ocr_events[0].data.get("allowed") is True


# --- failures -------------------------------------------------------------------------------------


def test_ac_a_10_llm_host_down_propagates_and_leaves_the_receipt_processing() -> None:
    """AC-A-10 (exit 4) / S3-AC-10: LlmUnavailableError is not a failure of the receipt."""
    llm = ScriptedLlm(LlmUnavailableError("cannot reach the LLM host llm-host.test:11434"))
    harness = build_harness(llm)

    with pytest.raises(LlmUnavailableError):
        harness.process_image()

    receipt = harness.receipt(1)
    assert receipt.status is ReceiptStatus.PROCESSING
    assert harness.open_reviews() == ()


def test_s5_ac_05_a_denied_tool_fails_the_run_loudly_without_retrying() -> None:
    """S5-AC-05 / AC-B-02 (in-process): a denial is traced, fails the run, is not retried."""
    llm = ScriptedLlm(extraction())
    harness = build_harness(
        llm, wrap_tools=lambda gateway: DenyingToolGateway(gateway, "save_receipt")
    )

    outcome = harness.process_image()

    assert outcome.graph is None
    assert outcome.failure is not None
    assert "denied: role=supervisor tool=save_receipt" in outcome.failure
    assert llm.calls == 1
    receipt = harness.receipt(outcome.receipt_id)
    assert (receipt.status, receipt.stage) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    events = harness.events(outcome.receipt_id)
    denied = [event for event in events if event.kind is TOOL and event.name == "save_receipt"]
    assert [event.data.get("allowed") for event in denied] == [False]
    assert denied[0].agent is AgentRole.SUPERVISOR
    assert events[-1].kind is TraceKind.ERROR
    assert "denied" in events[-1].summary
