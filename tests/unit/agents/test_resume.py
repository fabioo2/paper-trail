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
- Product codes (barcode-lookup plan A2, Q6; PC-TC-72): new threads carry ``state_version`` 2.
  Resuming a thread saved without it whose next node is ``commit_matches``, ``enrich`` or
  ``commit_enrichment`` raises ``OutdatedCheckpointError`` ("run N was saved by an older version;
  reprocess the receipt") before anything runs; a thread saved before ``match`` resumes. A
  ``created`` entry without ``catalog`` is a product without an entry. "Saved by the old version"
  is arranged by rewriting the thread's latest checkpoint without the key.
"""

import copy
from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from paper_trail.agents.graph import build_receipt_graph
from paper_trail.agents.state import STATE_VERSION, thread_id
from paper_trail.application.process_receipt import ProcessingStoppedError, ProcessOutcome
from paper_trail.domain.enums import MatchSource, ProcessingStage, ReceiptStatus, ReviewStatus
from paper_trail.domain.errors import ConflictError, NotFoundError
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.agents import GraphOutcome, OutdatedCheckpointError
from paper_trail.ports.llm import LlmUnavailableError
from paper_trail.ports.tools import ToolGateway
from tests.agent_harness import (
    Harness,
    agent_settings,
    build_harness,
)
from tests.extractions import (
    COSTCO_OCR,
    L3,
    LINE3_ISSUE,
    coded_extraction,
    extraction,
    inconsistent_extraction,
)
from tests.factories import receipt
from tests.fakes.catalog import TEST_APPLE, FakeCatalog
from tests.fakes.embeddings import FakeEmbeddings
from tests.fakes.llm import Reply, ScriptedLlm
from tests.fakes.tools import CrashAfterToolGateway
from tests.storage import in_unit
from tests.support import require_id

GPU_DOWN = "cannot reach the LLM host llm-host.test:11434"


APPLES = FakeCatalog({"3101": TEST_APPLE})


def _first(
    *replies: Reply,
    embeddings: FakeEmbeddings | None = None,
    wrap_tools: Callable[[ToolGateway], ToolGateway] | None = None,
    max_text_attempts: int = 2,
    enrichment: bool = False,
    catalog: FakeCatalog | None = None,
) -> Harness:
    return build_harness(
        ScriptedLlm(*replies),
        settings=agent_settings(max_text_attempts=max_text_attempts, enrichment=enrichment),
        embeddings=embeddings,
        wrap_tools=wrap_tools,
        catalog=catalog,
    )


def _second(first: Harness, *replies: Reply, catalog: FakeCatalog | None = None) -> Harness:
    """Another process on the same data: database, checkpoints and images."""
    return build_harness(
        ScriptedLlm(*replies),
        settings=first.settings,
        checkpointer=first.checkpointer,
        uow_factory=first.uow_factory,
        images=first.images,
        catalog=catalog,
    )


def _config(receipt_id: int) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id(receipt_id), "checkpoint_ns": ""}}


def _saved_values(saver: BaseCheckpointSaver[Any], receipt_id: int) -> dict[str, Any]:
    saved = saver.get_tuple(_config(receipt_id))
    assert saved is not None
    values: dict[str, Any] = saved.checkpoint["channel_values"]
    return values


def _rewrite_latest(
    saver: BaseCheckpointSaver[Any], receipt_id: int, change: Callable[[dict[str, Any]], None]
) -> None:
    """Replace the thread's latest checkpoint (same id) by one whose values ``change`` edited:
    what an older version would have saved. A key ``change`` drops loses its channel too."""
    saved = saver.get_tuple(_config(receipt_id))
    assert saved is not None
    checkpoint = copy.copy(saved.checkpoint)
    values = dict(checkpoint["channel_values"])
    change(values)
    versions = {k: v for k, v in checkpoint["channel_versions"].items() if k in values}
    checkpoint["channel_values"] = values
    checkpoint["channel_versions"] = versions
    saver.put(saved.parent_config or _config(receipt_id), checkpoint, saved.metadata, versions)


def _without_state_version(values: dict[str, Any]) -> None:
    del values["state_version"]


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


def test_ac_c_05_a_drop_during_matching_resumes_the_commit_only() -> None:
    """Matching embeds only a confirmed new product (a PLU-named one), so that is where the
    embedding host can drop."""
    down = FakeEmbeddings(error=LlmUnavailableError(GPU_DOWN))
    first = _first(coded_extraction([L3]), embeddings=down, catalog=APPLES)
    with pytest.raises(ProcessingStoppedError) as stopped:
        first.process_text(COSTCO_OCR)
    receipt_id = stopped.value.receipt_id
    saved = first.receipt(receipt_id)
    # Slice C review note 1: not finished while its lines are unlinked; it waits for the GPU.
    assert (saved.status, saved.stage) == (
        ReceiptStatus.PROCESSING,
        ProcessingStage.WAITING_FOR_GPU,
    )
    assert first.match_sources(receipt_id) == [None]
    assert first.product_ids() == []
    second = _second(first, catalog=APPLES)

    resumed = _graph(second.resume(receipt_id))

    assert resumed.resumed_from == "match"  # the embedding is in save_matches now
    assert resumed.status is ReceiptStatus.COMPLETED
    assert second.llm.calls == 0
    assert second.match_sources(receipt_id) == [MatchSource.NEW]
    assert len(second.product_ids()) == 1
    assert second.embeddings.calls == 1
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


# --- PC-TC-72: threads saved before product codes (A2, Q6) ---------------------------------------


def test_pc_tc_72_new_threads_carry_the_state_version() -> None:
    first = _first(extraction())

    paused = _graph(first.process_text(COSTCO_OCR, interrupt_after="extract_text"))

    assert _saved_values(first.checkpointer, paused.receipt_id)["state_version"] == STATE_VERSION
    assert STATE_VERSION == 2


@pytest.mark.parametrize(
    ("paused_after", "next_node"),
    [("match", "commit_matches"), ("commit_matches", "enrich"), ("enrich", "commit_enrichment")],
)
def test_pc_tc_72_an_old_thread_past_match_is_refused_before_anything_runs(
    paused_after: str, next_node: str
) -> None:
    first = _first(coded_extraction([L3]), enrichment=True, catalog=APPLES)
    receipt_id = _graph(first.process_text(COSTCO_OCR, interrupt_after=paused_after)).receipt_id
    _rewrite_latest(first.checkpointer, receipt_id, _without_state_version)
    second = _second(first, catalog=APPLES)
    graph = build_receipt_graph(first.deps, first.checkpointer)
    assert graph.get_state(_config(receipt_id)).next == (next_node,)
    checkpoints = list(first.checkpointer.list(_config(receipt_id)))
    values = _saved_values(first.checkpointer, receipt_id)
    receipt_before = first.receipt(receipt_id)
    products_before = [first.product(pid) for pid in first.product_ids()]

    with pytest.raises(OutdatedCheckpointError) as refused:
        second.resume(receipt_id)

    assert str(refused.value) == (
        f"run {receipt_id} was saved by an older version; reprocess the receipt"
    )
    assert list(first.checkpointer.list(_config(receipt_id))) == checkpoints
    assert _saved_values(first.checkpointer, receipt_id) == values
    assert first.receipt(receipt_id) == receipt_before
    assert [first.product(pid) for pid in first.product_ids()] == products_before
    assert second.llm.calls == 0
    assert second.events(receipt_id) == ()


@pytest.mark.parametrize("paused_after", ["extract_text", "persist"])
def test_pc_tc_72_an_old_thread_saved_before_match_resumes(paused_after: str) -> None:
    first = _first(coded_extraction([L3]), catalog=APPLES)
    receipt_id = _graph(first.process_text(COSTCO_OCR, interrupt_after=paused_after)).receipt_id
    _rewrite_latest(first.checkpointer, receipt_id, _without_state_version)
    second = _second(first, catalog=APPLES)

    resumed = _graph(second.resume(receipt_id))

    assert resumed.status is ReceiptStatus.COMPLETED
    assert second.match_sources(receipt_id) == [MatchSource.NEW]
    assert second.llm.calls == 0


def test_pc_tc_72_an_old_thread_waiting_for_a_review_is_resolved() -> None:
    first = _first(inconsistent_extraction(), max_text_attempts=1)
    parked = _graph(first.process_text(COSTCO_OCR))
    _rewrite_latest(first.checkpointer, parked.receipt_id, _without_state_version)
    second = _second(first)

    resolved = second.resolve(
        require_id(parked.review_id), force=True, reason="checked against the paper receipt"
    )

    assert resolved.status is ReceiptStatus.COMPLETED
    assert second.match_sources(parked.receipt_id) == [MatchSource.NEW] * 3
    assert second.llm.calls == 0


def test_pc_tc_72_a_created_entry_without_catalog_is_no_entry() -> None:
    first = _first(coded_extraction([L3]), enrichment=True, catalog=APPLES)
    receipt_id = _graph(first.process_text(COSTCO_OCR, interrupt_after="commit_matches")).receipt_id

    def without_catalog(values: dict[str, Any]) -> None:
        values["created"] = [
            {key: value for key, value in entry.items() if key != "catalog"}
            for entry in values["created"]
        ]

    _rewrite_latest(first.checkpointer, receipt_id, without_catalog)
    second = _second(first, catalog=APPLES)

    resumed = _graph(second.resume(receipt_id))

    assert resumed.status is ReceiptStatus.COMPLETED
    describes = [e for e in second.events(receipt_id) if e.name == "describe_from_catalog"]
    assert describes == []
    (apple,) = second.product_ids()
    assert second.product(apple).category is None


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
