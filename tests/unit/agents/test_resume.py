"""Checkpointer resume, GPU drops and reprocessing (agent core slice C): AC-C-04 / S3-AC-08,
AC-C-05 / S3-AC-10, and ``process --reprocess`` (PLAN 6 "Checkpointer resume from the CLI").

A second ``build_harness`` over the same unit of work, checkpointer and images stands for a
second process (``paper-trail process --resume ID`` after the first one stopped). In-process
tools; the MCP transport is compared in ``test_transport_equivalence.py`` (S5-AC-04).

Pinned (docs/tests/agent-core-C.md):
- ``ReceiptGraphRunner.start(..., interrupt_after: str | None = None)``: LangGraph's static
  breakpoint after that node. The run returns ``GraphOutcome(paused_at=<node>)`` with the
  receipt's saved status, PROCESSING when ``persist`` has not run yet, and no review.
- ``GraphOutcome`` gains ``resumed_from: str | None = None``: for a run that continued a thread,
  the last node that completed before it (``extract_text`` after a breakpoint there,
  ``supervisor`` when the next extraction attempt raised, ``persist`` when matching raised).
- ``ProcessReceiptFile.__call__(source, *, reprocess=False, interrupt_after=None)`` and
  ``ProcessReceiptFile.resume(receipt_id) -> ProcessOutcome``. ``resume`` continues the receipt's
  thread from its last checkpoint (``ReceiptGraphRunner.resume(id)``); a run that raises
  ``LlmUnavailableError`` again is a ``ProcessingStoppedError``, any other failure marks the
  receipt FAILED and is returned, like ``__call__``. NotFoundError for an unknown receipt;
  ConflictError when the receipt has an OPEN review (the message names ``review resolve``) and,
  from the runner, when its thread is absent or finished.
- ``LlmUnavailableError`` while the receipt is still PROCESSING sets its stage to
  WAITING_FOR_GPU (S3-AC-10); a receipt already COMPLETED keeps its status and stage.
- Slice C review note 1 (docs/tests/agent-core-D.md): ``persist`` saves an accepted draft
  PROCESSING/MATCHING, so a drop during matching leaves it PROCESSING (WAITING_FOR_GPU), never
  a "finished" receipt with unlinked lines. ``save_matches`` leaves the receipt COMPLETED/DONE,
  so a run resumed after a crash that marked it FAILED ends COMPLETED (``save_matches`` is
  idempotent, see the matching tools contract).
- ``reprocess=True`` for an image already stored: the receipt's OPEN review (if any) is
  DISMISSED, the thread is dropped (``runner.reset``), the receipt is set PROCESSING/READING and
  a new run starts from the stored OCR text (no ``ocr_read``; from the image when none is
  stored). ``duplicate`` is False and the receipt id is the same; ``save_receipt`` replaces the
  contents, so prices are not duplicated.
"""

from collections.abc import Callable

import pytest

from paper_trail.application.process_receipt import ProcessingStoppedError, ProcessOutcome
from paper_trail.domain.enums import MatchSource, ProcessingStage, ReceiptStatus, ReviewStatus
from paper_trail.domain.errors import ConflictError, NotFoundError
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.agents import GraphOutcome
from paper_trail.ports.llm import LlmUnavailableError
from paper_trail.ports.tools import ToolGateway
from tests.agent_harness import (
    Harness,
    agent_settings,
    build_harness,
)
from tests.extractions import (
    COSTCO_OCR,
    LINE3_ISSUE,
    extraction,
    inconsistent_extraction,
)
from tests.factories import receipt
from tests.fakes.embeddings import FakeEmbeddings
from tests.fakes.llm import Reply, ScriptedLlm
from tests.fakes.tools import CrashAfterToolGateway
from tests.storage import in_unit
from tests.support import require_id

GPU_DOWN = "cannot reach the LLM host llm-host.test:11434"


def _first(
    *replies: Reply,
    embeddings: FakeEmbeddings | None = None,
    wrap_tools: Callable[[ToolGateway], ToolGateway] | None = None,
    max_text_attempts: int = 2,
) -> Harness:
    return build_harness(
        ScriptedLlm(*replies),
        settings=agent_settings(max_text_attempts=max_text_attempts),
        embeddings=embeddings,
        wrap_tools=wrap_tools,
    )


def _second(first: Harness, *replies: Reply) -> Harness:
    """Another process on the same data: database, checkpoints and images."""
    return build_harness(
        ScriptedLlm(*replies),
        settings=first.settings,
        checkpointer=first.checkpointer,
        uow_factory=first.uow_factory,
        images=first.images,
    )


def _graph(outcome: ProcessOutcome) -> GraphOutcome:
    assert outcome.failure is None, outcome.failure
    assert outcome.graph is not None
    return outcome.graph


# --- AC-C-04 / S3-AC-08: --interrupt-after, then --resume --------------------------------------


def test_ac_c_04_a_breakpoint_after_extraction_pauses_with_the_checkpoint() -> None:
    first = _first(extraction())

    paused = _graph(first.process_text(COSTCO_OCR, interrupt_after="extract_text"))

    assert paused.paused_at == "extract_text"
    assert (paused.status, paused.review_id, paused.resumed_from) == (
        ReceiptStatus.PROCESSING,
        None,
        None,
    )
    assert first.node_runs(paused.receipt_id) == ["extract_text"]
    assert first.receipt(paused.receipt_id).status is ReceiptStatus.PROCESSING
    assert first.llm.calls == 1


def test_ac_c_04_s3_ac_08_resume_does_not_repeat_the_extraction() -> None:
    first = _first(extraction())
    receipt_id = first.process_text(COSTCO_OCR, interrupt_after="extract_text").receipt_id
    second = _second(first)  # no scripted reply: an extraction call would fail the test

    resumed = _graph(second.resume(receipt_id))

    assert second.llm.calls == 0
    assert resumed.status is ReceiptStatus.COMPLETED
    assert (resumed.resumed_from, resumed.paused_at, resumed.text_attempts) == (
        "extract_text",
        None,
        1,
    )
    assert second.node_runs(receipt_id)[:2] == ["validate", "supervisor"]
    assert second.receipt(receipt_id).status is ReceiptStatus.COMPLETED
    assert second.match_sources(receipt_id) == [MatchSource.NEW] * 3


def test_a_breakpoint_after_read_skips_the_ocr_on_resume() -> None:
    first = _first(extraction())
    receipt_id = first.process_image(interrupt_after="read").receipt_id
    second = _second(first, extraction())

    resumed = _graph(second.resume(receipt_id))

    assert resumed.resumed_from == "read"
    assert len(first.ocr.images) == 1
    assert second.ocr.images == []
    assert (TraceKind.TOOL, "ocr_read") not in second.steps(receipt_id)


def test_a_breakpoint_after_persist_reports_the_saved_status() -> None:
    """Slice C review note 1: ``persist`` saves the receipt PROCESSING/MATCHING; only
    ``save_matches`` makes it COMPLETED/DONE, so a receipt with unlinked lines never looks
    finished."""
    first = _first(extraction())

    paused = _graph(first.process_text(COSTCO_OCR, interrupt_after="persist"))

    assert (paused.paused_at, paused.status) == ("persist", ReceiptStatus.PROCESSING)
    stored = first.receipt(paused.receipt_id)
    assert (stored.status, stored.stage) == (ReceiptStatus.PROCESSING, ProcessingStage.MATCHING)
    assert first.match_sources(paused.receipt_id) == [None, None, None]
    resumed = _graph(_second(first).resume(paused.receipt_id))
    assert resumed.resumed_from == "persist"
    assert resumed.status is ReceiptStatus.COMPLETED
    assert first.match_sources(paused.receipt_id) == [MatchSource.NEW] * 3
    done = first.receipt(paused.receipt_id)
    assert (done.status, done.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)


def test_review_note_1_the_receipt_stays_matching_until_commit_matches() -> None:
    first = _first(extraction())

    paused = _graph(first.process_text(COSTCO_OCR, interrupt_after="match"))

    assert (paused.paused_at, paused.status) == ("match", ReceiptStatus.PROCESSING)
    stored = first.receipt(paused.receipt_id)
    assert (stored.status, stored.stage) == (ReceiptStatus.PROCESSING, ProcessingStage.MATCHING)
    assert stored.total is not None  # the contents are saved; only the links are missing


# --- AC-C-05 / S3-AC-10: the GPU drops mid-graph ------------------------------------------------


def test_ac_c_05_a_gpu_drop_keeps_the_checkpoint_and_waits_for_the_gpu() -> None:
    first = _first(inconsistent_extraction(), LlmUnavailableError(GPU_DOWN))

    with pytest.raises(ProcessingStoppedError) as stopped:
        first.process_text(COSTCO_OCR)

    receipt_id = stopped.value.receipt_id
    assert isinstance(stopped.value, LlmUnavailableError)
    stored = first.receipt(receipt_id)
    assert (stored.status, stored.stage) == (
        ReceiptStatus.PROCESSING,
        ProcessingStage.WAITING_FOR_GPU,
    )
    assert first.open_reviews() == ()


def test_ac_c_05_s3_ac_10_resume_continues_from_the_last_completed_node() -> None:
    """The first attempt (and its validation) is not repeated: only the retry is asked for,
    and it still carries the first attempt's feedback from the checkpoint."""
    first = _first(inconsistent_extraction(), LlmUnavailableError(GPU_DOWN))
    with pytest.raises(ProcessingStoppedError) as stopped:
        first.process_text(COSTCO_OCR)
    receipt_id = stopped.value.receipt_id
    second = _second(first, extraction())

    resumed = _graph(second.resume(receipt_id))

    assert second.llm.calls == 1
    assert LINE3_ISSUE in second.llm.last_user_message(0)
    assert (resumed.status, resumed.text_attempts) == (ReceiptStatus.COMPLETED, 2)
    assert resumed.resumed_from == "supervisor"
    assert second.node_runs(receipt_id)[0] == "extract_text"
    stored = second.receipt(receipt_id)
    assert (stored.status, stored.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)


def test_ac_c_05_a_drop_during_matching_resumes_matching_only() -> None:
    down = FakeEmbeddings(error=LlmUnavailableError(GPU_DOWN))
    first = _first(extraction(), embeddings=down)
    with pytest.raises(ProcessingStoppedError) as stopped:
        first.process_text(COSTCO_OCR)
    receipt_id = stopped.value.receipt_id
    saved = first.receipt(receipt_id)
    # Slice C review note 1: not finished while its lines are unlinked; it waits for the GPU.
    assert (saved.status, saved.stage) == (
        ReceiptStatus.PROCESSING,
        ProcessingStage.WAITING_FOR_GPU,
    )
    assert first.match_sources(receipt_id) == [None, None, None]
    assert first.product_ids() == []
    second = _second(first)

    resumed = _graph(second.resume(receipt_id))

    assert resumed.resumed_from == "persist"
    assert resumed.status is ReceiptStatus.COMPLETED
    assert second.llm.calls == 0
    assert second.match_sources(receipt_id) == [MatchSource.NEW] * 3
    assert len(second.product_ids()) == 3
    done = second.receipt(receipt_id)
    assert (done.status, done.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)


def test_a_crash_after_save_matches_resumes_without_duplicates() -> None:
    first = _first(
        extraction(), wrap_tools=lambda gateway: CrashAfterToolGateway(gateway, "save_matches")
    )
    crashed = first.process_text(COSTCO_OCR)
    assert crashed.failure is not None
    assert first.receipt(crashed.receipt_id).status is ReceiptStatus.FAILED
    products = first.product_ids()

    resumed = _graph(_second(first).resume(crashed.receipt_id))

    assert resumed.status is ReceiptStatus.COMPLETED
    stored = first.receipt(crashed.receipt_id)
    assert (stored.status, stored.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert first.product_ids() == products
    assert [len(first.price_history(pid)) for pid in products] == [1, 1, 1]


# --- resume: what cannot be resumed -------------------------------------------------------------


def test_resuming_an_unknown_receipt_is_not_found() -> None:
    with pytest.raises(NotFoundError):
        _first().resume(404)


def test_a_finished_thread_has_nothing_to_resume() -> None:
    first = _first(extraction())
    receipt_id = _graph(first.process_text(COSTCO_OCR)).receipt_id

    with pytest.raises(ConflictError):
        _second(first).resume(receipt_id)


def test_a_receipt_without_a_thread_has_nothing_to_resume() -> None:
    harness = _first()
    imported = receipt(status=ReceiptStatus.PROCESSING, stage=ProcessingStage.READING)
    receipt_id = require_id(in_unit(harness.uow_factory, lambda uow: uow.receipts.add(imported)).id)

    with pytest.raises(ConflictError):
        harness.resume(receipt_id)


def test_a_receipt_waiting_for_review_is_resolved_not_resumed() -> None:
    first = _first(inconsistent_extraction(), max_text_attempts=1)
    parked = _graph(first.process_text(COSTCO_OCR))
    assert parked.status is ReceiptStatus.NEEDS_REVIEW

    with pytest.raises(ConflictError, match="review resolve"):
        _second(first).resume(parked.receipt_id)

    assert first.review(require_id(parked.review_id)).status is ReviewStatus.OPEN


# --- reprocess ----------------------------------------------------------------------------------


def test_reprocess_reruns_the_graph_on_the_stored_text() -> None:
    harness = _first(extraction(), extraction())
    receipt_id = _graph(harness.process_image("photo")).receipt_id
    products = harness.product_ids()
    events_before = len(harness.events(receipt_id))

    again = harness.process_image("photo", reprocess=True)

    graph = _graph(again)
    assert (again.receipt_id, again.duplicate) == (receipt_id, False)
    assert graph.status is ReceiptStatus.COMPLETED
    assert harness.llm.calls == 2
    assert len(harness.ocr.images) == 1  # the stored OCR text was reused
    rerun = harness.events(receipt_id)[events_before:]
    starts = [e.name for e in rerun if e.kind is TraceKind.NODE and e.data.get("phase") == "start"]
    assert starts[0] == "extract_text"
    assert harness.match_sources(receipt_id) == [MatchSource.ALIAS] * 3
    assert harness.product_ids() == products
    assert [len(harness.price_history(pid)) for pid in products] == [1, 1, 1]


def test_reprocess_dismisses_the_open_review_of_the_dropped_thread() -> None:
    harness = _first(inconsistent_extraction(), extraction(), max_text_attempts=1)
    parked = _graph(harness.process_image("photo"))
    review_id = require_id(parked.review_id)

    again = _graph(harness.process_image("photo", reprocess=True))

    assert again.status is ReceiptStatus.COMPLETED
    assert harness.review(review_id).status is ReviewStatus.DISMISSED
    assert harness.open_reviews() == ()


def test_without_reprocess_the_same_photo_is_a_duplicate() -> None:
    harness = _first(extraction())
    receipt_id = _graph(harness.process_image("photo")).receipt_id

    again = harness.process_image("photo")

    assert (again.receipt_id, again.duplicate, again.graph) == (receipt_id, True, None)
    assert harness.llm.calls == 1


def test_reprocess_of_a_new_photo_just_processes_it() -> None:
    harness = _first(extraction())

    outcome = harness.process_image("fresh", reprocess=True)

    assert _graph(outcome).status is ReceiptStatus.COMPLETED
    assert outcome.duplicate is False
