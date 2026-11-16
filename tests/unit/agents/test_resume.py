"""Checkpointer resume, stopped reads and reprocessing (agent core slice C: AC-C-04 / S3-AC-08;
claude-extraction plan EC, V7, V11: CE-TC-027, 028; ``process --reprocess``).

A second ``build_harness`` over the same unit of work, checkpointer and images stands for a
second process (``paper-trail process --resume ID`` after the first one stopped).

Pinned:
- ``process --interrupt-after NODE`` pauses with the checkpoint; ``resume`` continues from the
  last completed node and never repeats a read that completed (``resumed_from``).
- A read that stops the run (``ReaderError``) leaves the receipt FAILED with its checkpoint;
  resuming continues at the attempt that stopped: Haiku again for attempt 1, the retry model
  with the saved feedback for attempt 2. One receipt row either way.
- Threads carry ``state_version`` 3. A thread saved at an older version is refused before any
  read or write (``OutdatedCheckpointError``), naming ``review resolve`` and
  ``process --reprocess``; its open review can still be answered by a person.
- ``reprocess=True`` re-reads the stored photo with Claude on a new thread, keeps the receipt id,
  dismisses the receipt's open review and replaces its contents (no duplicate prices).
"""

import contextlib
import copy
from collections.abc import Callable
from typing import Any

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver
from PIL import UnidentifiedImageError

from paper_trail.agents.state import STATE_VERSION
from paper_trail.domain.enums import MatchSource, ProcessingStage, ReceiptStatus, ReviewStatus
from paper_trail.domain.errors import ConflictError, NotFoundError
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.agents import OutdatedCheckpointError
from paper_trail.ports.reader import ReaderError, ReaderUnavailableError
from tests.agent_harness import EXTRACT_MODEL, RETRY_MODEL, Harness, agent_settings, build_harness
from tests.extractions import STORE_NAME, answer, inconsistent_answer
from tests.factories import receipt, receipt_draft
from tests.fakes.images import fake_png
from tests.fakes.reader import Reply, ScriptedReader
from tests.fakes.tools import CrashAfterToolGateway
from tests.storage import in_unit
from tests.support import require_id
from tests.unit.agents.graph_support import config, graph, saved_values, scripted, second

API_DOWN = "cannot reach the Claude API"


def _first(*replies: Reply, max_attempts: int = 2) -> Harness:
    return build_harness(
        ScriptedReader(*replies), settings=agent_settings(max_attempts=max_attempts)
    )


def _rewrite_latest(
    saver: BaseCheckpointSaver[Any], receipt_id: int, change: Callable[[dict[str, Any]], None]
) -> None:
    """Replace the thread's latest checkpoint (same id) by one whose values ``change`` edited:
    what an older version would have saved."""
    saved = saver.get_tuple(config(receipt_id))
    assert saved is not None
    checkpoint = copy.copy(saved.checkpoint)
    values = dict(checkpoint["channel_values"])
    change(values)
    versions = {k: v for k, v in checkpoint["channel_versions"].items() if k in values}
    checkpoint["channel_values"] = values
    checkpoint["channel_versions"] = versions
    saver.put(saved.parent_config or config(receipt_id), checkpoint, saved.metadata, versions)


def _at_version_2(values: dict[str, Any]) -> None:
    values["state_version"] = 2


def _stopped(harness: Harness) -> int:
    with pytest.raises(ReaderError):
        harness.process_image()
    return 1


# --- AC-C-04 / S3-AC-08: --interrupt-after, then --resume --------------------------------------


def test_ac_c_04_a_breakpoint_after_extract_pauses_with_the_checkpoint() -> None:
    first = _first(answer())

    paused = graph(first.process_image(interrupt_after="extract"))

    assert (paused.paused_at, paused.status, paused.review_id, paused.resumed_from) == (
        "extract",
        ReceiptStatus.PROCESSING,
        None,
        None,
    )
    assert first.node_runs(paused.receipt_id) == ["extract"]
    assert scripted(first).calls == 1


def test_ac_c_04_s3_ac_08_resume_does_not_repeat_the_read() -> None:
    first = _first(answer())
    receipt_id = first.process_image(interrupt_after="extract").receipt_id
    later = second(first)

    resumed = graph(later.resume(receipt_id))

    assert scripted(later).calls == 0
    assert (resumed.status, resumed.resumed_from, resumed.paused_at, resumed.attempts) == (
        ReceiptStatus.COMPLETED,
        "extract",
        None,
        1,
    )
    assert later.node_runs(receipt_id)[:2] == ["validate", "supervisor"]
    assert later.match_sources(receipt_id) == [MatchSource.NEW] * 3


def test_a_breakpoint_after_persist_reports_the_saved_status() -> None:
    """``persist`` saves the receipt PROCESSING/MATCHING; only ``save_matches`` makes it
    COMPLETED/DONE, so a receipt with unlinked lines never looks finished."""
    first = _first(answer())

    paused = graph(first.process_image(interrupt_after="persist"))

    assert (paused.paused_at, paused.status) == ("persist", ReceiptStatus.PROCESSING)
    stored = first.receipt(paused.receipt_id)
    assert (stored.status, stored.stage) == (ReceiptStatus.PROCESSING, ProcessingStage.MATCHING)
    assert first.match_sources(paused.receipt_id) == [None, None, None]
    resumed = graph(second(first).resume(paused.receipt_id))
    assert (resumed.resumed_from, resumed.status) == ("persist", ReceiptStatus.COMPLETED)
    done = first.receipt(paused.receipt_id)
    assert (done.status, done.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)


# --- CE-TC-027: resume continues, it does not start over ------------------------------------------


def test_ce_tc_027_a_stop_on_attempt_1_resumes_on_the_extract_model() -> None:
    first = _first(ReaderUnavailableError(API_DOWN))
    receipt_id = _stopped(first)
    later = second(first, answer())

    resumed = graph(later.resume(receipt_id))

    assert scripted(later).models == [EXTRACT_MODEL]
    assert (resumed.status, resumed.attempts) == (ReceiptStatus.COMPLETED, 1)
    stored = later.receipt(receipt_id)
    assert (stored.status, stored.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert in_unit(later.uow_factory, lambda uow: uow.receipts.get(receipt_id + 1)) is None


def test_ce_tc_027_a_stop_on_attempt_2_resumes_on_the_retry_model_with_the_feedback() -> None:
    first = _first(inconsistent_answer(), ReaderUnavailableError(API_DOWN))
    receipt_id = _stopped(first)
    later = second(first, answer())

    resumed = graph(later.resume(receipt_id))

    reader = scripted(later)
    assert reader.models == [RETRY_MODEL]
    assert reader.requests[0].text == scripted(first).requests[1].text
    assert "25.46" in reader.requests[0].text
    assert (resumed.status, resumed.attempts) == (ReceiptStatus.COMPLETED, 2)
    assert in_unit(later.uow_factory, lambda uow: uow.receipts.get(receipt_id + 1)) is None


def test_ce_tc_027_a_resume_that_stops_again_stays_resumable() -> None:
    first = _first(ReaderUnavailableError(API_DOWN))
    receipt_id = _stopped(first)
    still_down = second(first, ReaderUnavailableError(API_DOWN))

    with pytest.raises(ReaderError):
        still_down.resume(receipt_id)

    stored = first.receipt(receipt_id)
    assert (stored.status, stored.stage) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    assert graph(second(first, answer()).resume(receipt_id)).status is ReceiptStatus.COMPLETED


def test_r7_the_attempt_number_survives_a_resume() -> None:
    first = _first(inconsistent_answer(), ReaderUnavailableError(API_DOWN))
    receipt_id = _stopped(first)
    later = second(first, answer())

    graph(later.resume(receipt_id))

    assert [request.attempt for request in scripted(later).requests] == [2]
    reads = [event for event in later.events(receipt_id) if event.kind is TraceKind.LLM]
    assert [(event.data["model"], event.data["attempt"]) for event in reads] == [(RETRY_MODEL, 2)]


def test_a_failed_receipt_processed_again_without_resume_is_a_duplicate() -> None:
    harness = _first(ReaderUnavailableError(API_DOWN))
    receipt_id = _stopped(harness)

    again = harness.process_image()

    assert (again.receipt_id, again.duplicate, again.graph) == (receipt_id, True, None)
    assert scripted(harness).calls == 1
    assert harness.receipt(receipt_id).status is ReceiptStatus.FAILED


def test_a_crash_after_save_matches_resumes_without_duplicates() -> None:
    first = build_harness(
        ScriptedReader(answer()),
        wrap_tools=lambda gateway: CrashAfterToolGateway(gateway, "save_matches"),
    )
    crashed = first.process_image()
    assert crashed.failure is not None
    assert first.receipt(crashed.receipt_id).status is ReceiptStatus.FAILED
    products = first.product_ids()

    resumed = graph(second(first).resume(crashed.receipt_id))

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
    first = _first(answer())
    receipt_id = graph(first.process_image()).receipt_id

    with pytest.raises(ConflictError):
        second(first).resume(receipt_id)


def test_a_receipt_without_a_thread_has_nothing_to_resume() -> None:
    harness = _first()
    imported = receipt(status=ReceiptStatus.PROCESSING, stage=ProcessingStage.READING)
    receipt_id = require_id(in_unit(harness.uow_factory, lambda uow: uow.receipts.add(imported)).id)

    with pytest.raises(ConflictError):
        harness.resume(receipt_id)


def test_a_receipt_waiting_for_review_is_resolved_not_resumed() -> None:
    first = _first(inconsistent_answer(), max_attempts=1)
    parked = graph(first.process_image())

    with pytest.raises(ConflictError, match="review resolve"):
        second(first).resume(parked.receipt_id)

    assert first.review(require_id(parked.review_id)).status is ReviewStatus.OPEN


# --- CE-TC-028: a version-2 checkpoint is refused -------------------------------------------------


def test_ce_tc_028_new_threads_carry_state_version_3() -> None:
    first = _first(answer())

    paused = graph(first.process_image(interrupt_after="extract"))

    assert STATE_VERSION == 3
    assert saved_values(first.checkpointer, paused.receipt_id)["state_version"] == 3


@pytest.mark.parametrize("paused_after", ["extract", "commit_matches"])
def test_ce_tc_028_a_version_2_thread_is_refused_before_anything_runs(paused_after: str) -> None:
    first = build_harness(ScriptedReader(answer()), settings=agent_settings(enrichment=True))
    receipt_id = graph(first.process_image(interrupt_after=paused_after)).receipt_id
    _rewrite_latest(first.checkpointer, receipt_id, _at_version_2)
    checkpoints = list(first.checkpointer.list(config(receipt_id)))
    receipt_before = first.receipt(receipt_id)
    products_before = [first.product(pid) for pid in first.product_ids()]
    later = second(first)

    with pytest.raises(OutdatedCheckpointError) as refused:
        later.resume(receipt_id)

    message = str(refused.value)
    assert ("review resolve" in message, "--reprocess" in message) == (True, True)
    assert list(first.checkpointer.list(config(receipt_id))) == checkpoints
    assert first.receipt(receipt_id) == receipt_before
    assert [first.product(pid) for pid in first.product_ids()] == products_before
    assert (scripted(later).calls, later.events(receipt_id)) == (0, ())


def test_ce_tc_028_a_version_3_thread_resumes() -> None:
    first = _first(answer())
    receipt_id = graph(first.process_image(interrupt_after="persist")).receipt_id

    resumed = graph(second(first).resume(receipt_id))

    assert resumed.status is ReceiptStatus.COMPLETED


def test_ce_tc_028_an_old_threads_review_is_answered_by_a_persons_draft() -> None:
    """Its pending reading is in the old shape, so only a person's corrected draft answers it."""
    first = _first(inconsistent_answer(), max_attempts=1)
    parked = graph(first.process_image())
    _rewrite_latest(first.checkpointer, parked.receipt_id, _at_version_2)
    later = second(first)
    assert later.runner.pending_draft(parked.receipt_id) is None

    resolved = later.resolve(require_id(parked.review_id), receipt_draft(store_name=STORE_NAME))

    assert resolved.status is ReceiptStatus.COMPLETED
    assert later.match_sources(parked.receipt_id) == [MatchSource.NEW] * 2
    assert scripted(later).calls == 0


# --- reprocess ----------------------------------------------------------------------------------


def test_reprocess_reads_the_stored_photo_again_on_a_new_thread() -> None:
    harness = _first(answer(), answer())
    receipt_id = graph(harness.process_image("photo")).receipt_id
    products = harness.product_ids()

    again = harness.process_image("photo", reprocess=True)

    result = graph(again)
    assert (again.receipt_id, again.duplicate, result.status) == (
        receipt_id,
        False,
        ReceiptStatus.COMPLETED,
    )
    reader = scripted(harness)
    assert reader.models == [EXTRACT_MODEL, EXTRACT_MODEL]
    assert [request.image for request in reader.requests] == [fake_png("photo")] * 2
    assert harness.match_sources(receipt_id) == [MatchSource.ALIAS] * 3
    assert harness.product_ids() == products
    assert [len(harness.price_history(pid)) for pid in products] == [1, 1, 1]


def test_reprocess_dismisses_the_open_review_of_the_dropped_thread() -> None:
    harness = _first(inconsistent_answer(), answer(), max_attempts=1)
    parked = graph(harness.process_image("photo"))
    review_id = require_id(parked.review_id)

    again = graph(harness.process_image("photo", reprocess=True))

    assert again.status is ReceiptStatus.COMPLETED
    assert harness.review(review_id).status is ReviewStatus.DISMISSED
    assert harness.open_reviews() == ()


def test_reprocess_is_the_way_out_of_a_version_2_thread() -> None:
    harness = _first(answer(), answer())
    receipt_id = graph(harness.process_image("photo", interrupt_after="persist")).receipt_id
    _rewrite_latest(harness.checkpointer, receipt_id, _at_version_2)

    again = graph(harness.process_image("photo", reprocess=True))

    assert (again.receipt_id, again.status) == (receipt_id, ReceiptStatus.COMPLETED)
    assert scripted(harness).calls == 2


def test_reprocess_after_a_review_resolved_with_a_persons_draft_reads_again() -> None:
    harness = _first(inconsistent_answer(), answer(), max_attempts=1)
    parked = graph(harness.process_image("photo"))
    review_id = require_id(parked.review_id)
    harness.resolve(review_id, receipt_draft(store_name=STORE_NAME))

    again = graph(harness.process_image("photo", reprocess=True))

    assert (again.receipt_id, again.status) == (parked.receipt_id, ReceiptStatus.COMPLETED)
    assert scripted(harness).calls == 2
    assert harness.review(review_id).status is ReviewStatus.RESOLVED
    assert [line.line_total.amount_cents for line in harness.receipt(again.receipt_id).items] == [
        899,
        658,
        998,
    ]


@pytest.mark.parametrize(
    "failure",
    [
        ReaderUnavailableError(API_DOWN),
        UnidentifiedImageError("cannot identify image file"),
    ],
    ids=["claude-down-R8", "stored-image-corrupt-R16"],
)
def test_r8_r16_a_reprocess_that_stops_before_persist_keeps_the_receipts_previous_state(
    failure: Exception,
) -> None:
    """The adapter decodes the stored photo before the request, so a corrupt one fails the
    read with Pillow's error, not a ``ReaderError``."""
    harness = _first(answer(), failure)
    receipt_id = graph(harness.process_image("photo")).receipt_id
    before = harness.receipt(receipt_id)

    with contextlib.suppress(ReaderError):
        harness.process_image("photo", reprocess=True)

    after = harness.receipt(receipt_id)
    assert (after.status, after.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert after.items == before.items


def test_without_reprocess_the_same_photo_is_a_duplicate() -> None:
    harness = _first(answer())
    receipt_id = graph(harness.process_image("photo")).receipt_id

    again = harness.process_image("photo")

    assert (again.receipt_id, again.duplicate, again.graph) == (receipt_id, True, None)
    assert scripted(harness).calls == 1
