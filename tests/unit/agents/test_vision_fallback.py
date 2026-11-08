"""The vision fallback (agent core slice D): AC-D-01 = S3-AC-04 in full, AC-D-03, and a GPU drop
during the vision attempt (S3-AC-10).

Scripted LLM, in-process tools, fakes; the vision request is checked as the adapter would send
it (the Ollama adapter's base64 encoding of ``ChatMessage.images`` is S3-AC-01, already covered
in ``tests/unit/adapters/test_ollama_llm.py``).

Pinned (docs/tests/agent-core-D.md):
- Graph: ``supervisor -> extract_vision -> validate``. ``extract_vision`` is the EXTRACTION agent;
  ``NODE_NAMES`` gains it after ``extract_text``. The supervisor routes there (``route()``, as in
  slice A) when the latest attempt is invalid or unparsed, the text attempts are used up and
  vision attempts are left; ``graph_limits(settings).max_vision_attempts`` is
  ``GRAPH__MAX_VISION_ATTEMPTS`` (default 1) and no longer forced to 0.
- A receipt without an image (``--text-file``: ``image_path`` None) is routed as if
  ``max_vision_attempts`` were 0 (AC-D-03).
- ``AgentDeps`` gains ``images: ImageStore``; the vision attempt reads the photo with
  ``ImageStore.open(image_path)``.
- The vision request: ``settings.ollama.vision_model``, temperature 0. Since extraction
  accuracy (docs/tests/extraction-accuracy.md) it uses its own prompt ``vision/v1``: system and
  one user message, no examples, no OCR text; the final user message carries exactly the
  stored photo's bytes. Its answer is a printed-value answer (``PrintedExtraction``, scripted
  with ``printed_extraction()``); one that never validates is an unparsed attempt. Each vision
  attempt adds 1 to ``vision_attempts``.
- The first vision attempt gets no feedback (a clean prompt: the text attempts' numbers must
  not anchor the vision model); a vision retry gets the supervisor's feedback about the
  previous vision answer (``test_vision_first.py``).
- The LLM trace event of a vision attempt: agent EXTRACTION, name the prompt tag, ``data["model"]``
  the vision model; ``data["messages"]`` never holds image bytes: a message with images has
  ``"images"``, the SHA-256 hex digest of each image, and the others have no ``"images"`` key.
"""

import base64
import hashlib
import json

import pytest

from paper_trail.agents.graph import NODE_NAMES
from paper_trail.application.process_receipt import ProcessingStoppedError, ProcessOutcome
from paper_trail.domain.enums import (
    AgentRole,
    ProcessingStage,
    ReceiptStatus,
    ReviewReason,
    ReviewStatus,
)
from paper_trail.domain.models import JsonValue
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.agents import GraphOutcome
from paper_trail.ports.llm import LlmUnavailableError
from tests.agent_harness import (
    TEXT_MODEL,
    VISION_MODEL,
    Harness,
    agent_settings,
    build_harness,
)
from tests.extractions import (
    COSTCO_OCR,
    GENERIC_RETRY_LINE,
    LINE1_ISSUE,
    LINE3_ISSUE,
    SUBTOTAL_ISSUE,
    extraction,
    inconsistent_extraction,
    printed_extraction,
    printed_inconsistent_extraction,
)
from tests.fakes.images import fake_png
from tests.fakes.llm import Reply, ScriptedLlm
from tests.support import require_id

LABEL = "faded-costco"
PHOTO = fake_png(LABEL)
GPU_DOWN = "cannot reach the LLM host llm-host.test:11434"
ROUTE = TraceKind.ROUTE


def _harness(
    *replies: Reply,
    then: Reply | None = None,
    feedback: bool = True,
    examples: bool = True,
    max_vision_attempts: int = 1,
) -> Harness:
    """Vision on (the production default of one attempt), enrichment off."""
    settings = agent_settings(
        max_vision_attempts=max_vision_attempts, feedback=feedback, examples=examples
    )
    return build_harness(ScriptedLlm(*replies, then=then), settings=settings)


def _graph(outcome: ProcessOutcome) -> GraphOutcome:
    assert outcome.failure is None, outcome.failure
    assert outcome.graph is not None
    return outcome.graph


def _routes(harness: Harness, receipt_id: int) -> list[str]:
    return [name for kind, name in harness.steps(receipt_id) if kind is ROUTE]


def _review_messages(harness: Harness, review_id: int | None) -> list[JsonValue]:
    issues = harness.review(require_id(review_id)).details["issues"]
    assert isinstance(issues, list)
    return [issue["message"] for issue in issues if isinstance(issue, dict)]


def test_extract_vision_is_a_node_of_the_graph() -> None:
    assert "extract_vision" in NODE_NAMES
    assert NODE_NAMES.index("extract_vision") == NODE_NAMES.index("extract_text") + 1


# --- AC-D-01 = S3-AC-04 ---------------------------------------------------------------------------


def test_s3_ac_04_two_text_attempts_then_one_vision_attempt_then_review() -> None:
    """Always inconsistent: two text attempts, one vision attempt whose request includes the
    image, then the graph interrupts; the review holds the LAST attempt's issues; the receipt
    is parked (NEEDS_REVIEW), not failed."""
    harness = _harness(
        inconsistent_extraction(),
        inconsistent_extraction(),
        printed_inconsistent_extraction(line=1),
    )

    outcome = harness.process_image(LABEL)

    graph = _graph(outcome)
    assert harness.llm.calls == 3
    assert (graph.text_attempts, graph.vision_attempts, graph.paused_at) == (2, 1, None)
    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    assert graph.review_reason is ReviewReason.VALIDATION_FAILED
    review = harness.review(require_id(graph.review_id))
    assert (review.status, review.reason) == (ReviewStatus.OPEN, ReviewReason.VALIDATION_FAILED)
    assert _review_messages(harness, graph.review_id) == [LINE1_ISSUE, SUBTOTAL_ISSUE]
    stored = harness.receipt(outcome.receipt_id)
    assert (stored.status, stored.stage) == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
    )
    assert _routes(harness, outcome.receipt_id) == ["extract_text", "extract_vision", "review"]


def test_ac_d_01_the_vision_request_uses_the_vision_model_and_carries_the_photo() -> None:
    harness = _harness(inconsistent_extraction(), inconsistent_extraction(), printed_extraction())

    harness.process_image(LABEL)

    text_1, text_2, vision = harness.llm.requests
    assert (text_1.model, text_2.model) == (TEXT_MODEL, TEXT_MODEL)
    assert vision.model == VISION_MODEL
    assert vision.temperature == 0.0
    assert text_1.tag == "extraction/v1"
    assert vision.tag == "vision/v1"  # its own prompt (extraction accuracy)
    assert vision.messages[-1].role == "user"
    assert tuple(vision.messages[-1].images) == (PHOTO,)
    assert all(not message.images for message in vision.messages[:-1])
    assert all(not message.images for request in (text_1, text_2) for message in request.messages)
    # The photo alone: no examples and no OCR text for the vision model to copy.
    assert [message.role for message in vision.messages] == ["system", "user"]
    assert "<receipt_data>" not in vision.messages[-1].content
    assert all("KS ORG EGGS 24CT" not in message.content for message in vision.messages)


def test_ea_ac_09_the_vision_attempt_after_text_attempts_gets_a_clean_prompt() -> None:
    """Was ``test_ac_d_01_the_vision_attempt_carries_the_latest_issues``: the text attempts'
    issues anchored the vision model to text-invented numbers (evidence 4)."""
    harness = _harness(
        inconsistent_extraction(line=1), inconsistent_extraction(), printed_extraction()
    )

    harness.process_image(LABEL)

    final = harness.llm.last_user_message(2)
    assert "<validation_feedback>" not in final
    assert LINE3_ISSUE not in final
    assert LINE1_ISSUE not in final


def test_ea_ac_09_without_feedback_the_first_vision_attempt_gets_no_line_either() -> None:
    harness = _harness(
        inconsistent_extraction(), inconsistent_extraction(), printed_extraction(), feedback=False
    )

    harness.process_image(LABEL)

    final = harness.llm.last_user_message(2)
    assert GENERIC_RETRY_LINE not in final
    assert LINE3_ISSUE not in final
    assert GENERIC_RETRY_LINE in harness.llm.last_user_message(1)  # the text retry still has it


@pytest.mark.parametrize("examples", [True, False])
def test_ac_d_01_the_vision_request_never_has_example_pairs(examples: bool) -> None:
    harness = _harness(
        inconsistent_extraction(),
        inconsistent_extraction(),
        printed_extraction(),
        examples=examples,
    )

    harness.process_image(LABEL)

    vision = harness.llm.requests[2]
    assert vision.tag.startswith("vision/v1")
    assert [message.role for message in vision.messages] == ["system", "user"]


def test_ac_d_01_a_vision_read_that_adds_up_is_saved_and_matched() -> None:
    harness = _harness(inconsistent_extraction(), inconsistent_extraction(), printed_extraction())

    outcome = harness.process_image(LABEL)

    graph = _graph(outcome)
    assert graph.status is ReceiptStatus.COMPLETED
    assert (graph.text_attempts, graph.vision_attempts) == (2, 1)
    assert _routes(harness, outcome.receipt_id) == ["extract_text", "extract_vision", "persist"]
    runs = harness.node_runs(outcome.receipt_id)
    vision_at = runs.index("extract_vision")
    assert runs[vision_at : vision_at + 3] == ["extract_vision", "validate", "supervisor"]
    assert harness.steps(outcome.receipt_id)[-1] == (TraceKind.TOOL, "save_matches")
    assert harness.receipt(outcome.receipt_id).total is not None


def test_a_vision_answer_that_never_validates_ends_in_extraction_failed() -> None:
    harness = _harness(inconsistent_extraction(), inconsistent_extraction(), "not json at all")

    graph = _graph(harness.process_image(LABEL))

    assert graph.vision_attempts == 1
    assert graph.review_reason is ReviewReason.EXTRACTION_FAILED
    assert _review_messages(harness, graph.review_id) == []


def test_every_vision_attempt_allowed_is_made() -> None:
    harness = _harness(
        inconsistent_extraction(),
        inconsistent_extraction(),
        printed_inconsistent_extraction(),
        printed_inconsistent_extraction(),
        max_vision_attempts=2,
    )

    graph = _graph(harness.process_image(LABEL))

    models = [request.model for request in harness.llm.requests]
    assert models == [TEXT_MODEL, TEXT_MODEL, VISION_MODEL, VISION_MODEL]
    assert (graph.text_attempts, graph.vision_attempts) == (2, 2)
    assert graph.review_reason is ReviewReason.VALIDATION_FAILED


def test_ac_d_01_the_trace_names_the_vision_model_and_never_holds_the_image() -> None:
    harness = _harness(inconsistent_extraction(), inconsistent_extraction(), printed_extraction())

    outcome = harness.process_image(LABEL)

    llm_events = [
        event for event in harness.events(outcome.receipt_id) if event.kind is TraceKind.LLM
    ]
    vision = llm_events[2]
    assert (vision.agent, vision.name, vision.data["model"]) == (
        AgentRole.EXTRACTION,
        "vision/v1",
        VISION_MODEL,
    )
    messages = vision.data["messages"]
    assert isinstance(messages, list)
    *earlier, final = messages
    assert isinstance(final, dict)
    assert final["images"] == [hashlib.sha256(PHOTO).hexdigest()]
    assert all(isinstance(message, dict) and "images" not in message for message in earlier)
    dumped = json.dumps([dict(event.data) for event in harness.events(outcome.receipt_id)])
    assert base64.b64encode(PHOTO).decode("ascii") not in dumped


# --- AC-D-03: a text input never goes to vision ---------------------------------------------------


def test_ac_d_03_a_text_input_never_routes_to_vision() -> None:
    harness = _harness(then=inconsistent_extraction())

    graph = _graph(harness.process_text(COSTCO_OCR))

    assert harness.llm.calls == 2
    assert {request.model for request in harness.llm.requests} == {TEXT_MODEL}
    assert all(
        not message.images for request in harness.llm.requests for message in request.messages
    )
    assert (graph.text_attempts, graph.vision_attempts) == (2, 0)
    assert graph.review_reason is ReviewReason.VALIDATION_FAILED
    assert "extract_vision" not in _routes(harness, graph.receipt_id)
    assert "extract_vision" not in harness.node_runs(graph.receipt_id)


def test_ac_d_03_a_reprocessed_photo_may_still_use_vision() -> None:
    """Reprocessing starts from the stored OCR text, but the receipt still has its photo."""
    harness = _harness(
        extraction(),
        inconsistent_extraction(),
        inconsistent_extraction(),
        printed_inconsistent_extraction(),
    )
    first = _graph(harness.process_image(LABEL))
    assert first.status is ReceiptStatus.COMPLETED

    again = _graph(harness.process_image(LABEL, reprocess=True))

    assert again.vision_attempts == 1
    assert harness.llm.requests[-1].model == VISION_MODEL
    assert tuple(harness.llm.requests[-1].messages[-1].images) == (PHOTO,)


# --- S3-AC-10 across the vision attempt -----------------------------------------------------------


def test_a_gpu_drop_during_the_vision_attempt_resumes_with_the_vision_attempt_only() -> None:
    first = _harness(
        inconsistent_extraction(), inconsistent_extraction(), LlmUnavailableError(GPU_DOWN)
    )
    with pytest.raises(ProcessingStoppedError) as stopped:
        first.process_image(LABEL)
    receipt_id = stopped.value.receipt_id
    waiting = first.receipt(receipt_id)
    assert (waiting.status, waiting.stage) == (
        ReceiptStatus.PROCESSING,
        ProcessingStage.WAITING_FOR_GPU,
    )
    second = build_harness(
        ScriptedLlm(printed_extraction()),
        settings=first.settings,
        checkpointer=first.checkpointer,
        uow_factory=first.uow_factory,
        images=first.images,
    )

    resumed = _graph(second.resume(receipt_id))

    assert second.llm.calls == 1
    assert second.llm.requests[0].model == VISION_MODEL
    assert tuple(second.llm.requests[0].messages[-1].images) == (PHOTO,)
    assert resumed.resumed_from == "supervisor"
    assert (resumed.status, resumed.text_attempts, resumed.vision_attempts) == (
        ReceiptStatus.COMPLETED,
        2,
        1,
    )
