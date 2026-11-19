"""A person's answer to a review from the web app (``AnswerReview``), queued as a
``resolve_review`` job and applied by the worker (``ProcessReceiptJobs``), on a real SQLite file
with the graph wired from fakes (``tests/agent_harness``).

Pinned: an answer is checked as ``review resolve`` checks it, then queued with the receipt
PROCESSING/QUEUED and the review still OPEN (left out of the pending list); the worker resumes
the same thread with it (no new read) and the review is RESOLVED; card numbers never reach the
job; a second answer to the same review is a ConflictError and queues nothing; an answer the
worker cannot apply fails the job and gives the review back to a person, unless the review was
resolved by other means meanwhile; a force reason is masked; a receipt-level discount stays at
receipt level. Recovery: a worker that died after giving the answer has its job claimed again,
which continues the run (or, finished, only closes the review) without giving the answer again;
a run that fails after the answer was saved closes the review, fails the receipt (never a
COMPLETED one) and is continued by ``process --resume``.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

import pytest

from paper_trail.adapters.sqlite.job_queue import SqliteJobQueue
from paper_trail.adapters.sqlite.receipt_repository import SqliteReceiptRepository
from paper_trail.adapters.sqlite.review_queue import SqliteReviewQueue
from paper_trail.application.jobs import STOPPED_UNEXPECTEDLY, ProcessReceiptJobs
from paper_trail.application.process_receipt import RetryReceipt
from paper_trail.application.receipts import (
    REVIEW_ANSWER_JOB_ATTEMPTS,
    AnswerReview,
    FindOpenReview,
    GetPendingReview,
    InconsistentDraftError,
    ListPendingReviews,
)
from paper_trail.domain.enums import (
    AgentRole,
    Category,
    FailureReason,
    JobKind,
    JobStatus,
    ProcessingStage,
    ReceiptStatus,
    ReviewReason,
    ReviewStatus,
    Unit,
)
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import Job, JsonValue, ReceiptDraft, ReviewItem
from paper_trail.domain.money import Money
from paper_trail.domain.rules.arithmetic import IssueCode
from paper_trail.ports.reader import ReaderUnavailableError
from paper_trail.ports.tools import ToolDescription, ToolGateway
from tests.agent_harness import Harness, agent_settings, build_harness, receipt_hold
from tests.extractions import (
    NOW,
    PURCHASED_ON,
    answer,
    codex_answer,
    inconsistent_answer,
    item,
    receipt_answer,
)
from tests.factories import (
    MASKED_TEST_PAN,
    TEST_PAN,
    cad,
    line_item,
    receipt_draft,
    simple_draft,
)
from tests.fakes.clock import FakeClock
from tests.fakes.reader import ScriptedReader
from tests.fakes.tools import CrashAfterToolGateway
from tests.pans import card_numbers
from tests.storage import ScratchDatabase, scratch_database
from tests.support import require_id

WORKER = "worker-1"
LEASE = timedelta(minutes=30)


@dataclass
class Parked:
    harness: Harness
    receipt_id: int
    review_id: int
    answer: AnswerReview
    run_job: ProcessReceiptJobs
    database: ScratchDatabase

    @property
    def reader(self) -> ScriptedReader:
        assert isinstance(self.harness.reader, ScriptedReader)
        return self.harness.reader

    def state(self) -> tuple[ReceiptStatus, ProcessingStage, ReviewStatus]:
        receipt = self.harness.receipt(self.receipt_id)
        return receipt.status, receipt.stage, self.harness.review(self.review_id).status

    def jobs(self) -> list[Job]:
        found: list[Job] = []
        with self.harness.uow_factory(read_only=True) as uow:
            while (job := uow.jobs.get(len(found) + 1)) is not None:
                found.append(job)
        return found

    def pending(self) -> list[int]:
        listed = ListPendingReviews(self.harness.uow_factory, self.harness.runner)()
        return [require_id(pending.review.id) for pending in listed]


class StopOnce:
    """Tools that raise ``error`` at the first call of ``tool`` once armed: a worker that dies
    (a BaseException leaves its job RUNNING) or a run that fails after the answer was taken."""

    def __init__(self, error: BaseException, tool: str = "save_matches") -> None:
        self.error = error
        self.tool = tool
        self.armed = False
        self.inner: ToolGateway | None = None

    def wrap(self, inner: ToolGateway) -> ToolGateway:
        self.inner = inner
        return self

    def call(
        self, role: AgentRole, name: str, payload: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        if self.armed and name == self.tool:
            self.armed = False
            raise self.error
        assert self.inner is not None
        return self.inner.call(role, name, payload)

    def describe(self, role: AgentRole) -> tuple[ToolDescription, ...]:
        assert self.inner is not None
        return self.inner.describe(role)


class WorkerKilledError(BaseException):
    """The worker process died mid-job."""


def _park(stop: StopOnce | None = None, reading: dict[str, Any] | None = None) -> Parked:
    """Review of a receipt whose one reading came back 9 cents short (VALIDATION_FAILED), or
    of ``reading``."""
    harness, database = _harness(
        ScriptedReader(inconsistent_answer() if reading is None else reading),
        None if stop is None else stop.wrap,
    )
    graph = harness.process_image().graph
    assert graph is not None
    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    return _parked_at(harness, database, graph.receipt_id, require_id(graph.review_id))


def _harness(
    reader: ScriptedReader, wrap_tools: Callable[[ToolGateway], ToolGateway] | None
) -> tuple[Harness, ScratchDatabase]:
    clock = FakeClock(NOW)
    database = scratch_database(clock)
    harness = build_harness(
        reader,
        settings=agent_settings(max_attempts=1),
        clock=clock,
        uow_factory=database.uow_factory,
        wrap_tools=wrap_tools,
    )
    return harness, database


def _parked_at(
    harness: Harness, database: ScratchDatabase, receipt_id: int, review_id: int
) -> Parked:
    return Parked(
        database=database,
        harness=harness,
        receipt_id=receipt_id,
        review_id=review_id,
        answer=AnswerReview(harness.uow_factory, harness.resolve),
        run_job=ProcessReceiptJobs(
            harness.uow_factory,
            harness.process,
            harness.tracer,
            worker_id=WORKER,
            lease=LEASE,
            resolve_review=harness.resolve,
        ),
    )


@pytest.fixture
def parked() -> Parked:
    return _park()


def _corrected() -> ReceiptDraft:
    """What the person typed in: the receipt as it really is (consistent)."""
    return receipt_draft(store_name="Costco", ocr_text=None, image_path=None)


def test_a_queued_correction_is_saved_by_the_worker_on_the_same_thread(parked: Parked) -> None:
    calls = parked.reader.calls
    assert parked.pending() == [parked.review_id]

    queued = parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")

    assert (queued.status, queued.stage) == (ReceiptStatus.PROCESSING, ProcessingStage.QUEUED)
    assert parked.state() == (ReceiptStatus.PROCESSING, ProcessingStage.QUEUED, ReviewStatus.OPEN)
    assert parked.pending() == []
    (job,) = parked.jobs()
    assert (job.kind, job.receipt_id, job.status) == (
        JobKind.RESOLVE_REVIEW,
        parked.receipt_id,
        JobStatus.PENDING,
    )

    assert parked.run_job() is True

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    receipt = parked.harness.receipt(parked.receipt_id)
    assert (receipt.store_name, receipt.total) == ("Costco", cad(1635))
    assert parked.reader.calls == calls
    assert parked.jobs()[0].status is JobStatus.SUCCEEDED
    assert parked.run_job() is False


def test_a_forced_accept_keeps_the_reading_and_remembers_why(parked: Parked) -> None:
    parked.answer(parked.review_id, None, force=True, reason="  torn receipt ")
    parked.run_job()

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert parked.harness.receipt(parked.receipt_id).total == cad(2683)
    correction = parked.harness.latest_correction(f"receipt:{parked.receipt_id}.arithmetic")
    assert correction is not None
    assert correction.new_value == "torn receipt"


def test_an_answer_that_does_not_add_up_queues_nothing(parked: Parked) -> None:
    with pytest.raises(InconsistentDraftError):
        parked.answer(parked.review_id, None)

    assert parked.state() == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
        ReviewStatus.OPEN,
    )
    assert parked.jobs() == []


def test_card_numbers_in_an_answer_never_reach_the_job_or_the_correction(
    parked: Parked,
) -> None:
    wrong = replace(_corrected(), store_name=f"Costco {TEST_PAN}", total=cad(1600))
    parked.answer(parked.review_id, wrong, force=True, reason=f"paid with {TEST_PAN}")

    (job,) = parked.jobs()
    assert card_numbers(json.dumps(job.payload)) == []
    parked.run_job()
    correction = parked.harness.latest_correction(f"receipt:{parked.receipt_id}.arithmetic")
    assert correction is not None
    assert correction.new_value == f"paid with {MASKED_TEST_PAN}"


def test_an_answer_keeps_a_receipt_level_discount_at_receipt_level(parked: Parked) -> None:
    """The editor shows an unlinked TPD/1234567 as a receipt discount; the discount rule would
    link it to the eggs (code 1234567), changing what the eggs cost."""
    parked.answer(
        parked.review_id,
        simple_draft(ocr_text=None, image_path=None, store_name="Costco"),
        force=True,
        reason="checked the paper",
    )
    parked.run_job()

    lines = parked.harness.receipt(parked.receipt_id).items
    assert [(line.raw_text, line.applies_to_position) for line in lines][-1] == (
        "TPD/1234567",
        None,
    )
    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)


def test_a_worker_that_died_after_giving_the_answer_continues_the_run_without_giving_it_again() -> (
    None
):
    stop = StopOnce(WorkerKilledError())
    parked = _park(stop)
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    stop.armed = True

    with pytest.raises(WorkerKilledError):
        parked.run_job()
    assert not parked.harness.runner.waiting_for_review(parked.receipt_id)
    assert parked.jobs()[0].status is JobStatus.RUNNING
    parked.harness.clock.advance(LEASE + timedelta(seconds=1))

    assert parked.run_job() is True

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    receipt = parked.harness.receipt(parked.receipt_id)
    assert (receipt.store_name, receipt.total) == ("Costco", cad(1635))
    assert parked.jobs()[0].status is JobStatus.SUCCEEDED


def test_h4_04_an_answer_whose_workers_keep_dying_fails_the_receipt_and_try_again_finishes_it() -> (
    None
):
    """The graph took the answer before each worker died: the sweep fails the receipt and
    closes the review (no second answer can be taken and ignored); Try again resumes the
    thread, which completes it with the answer given."""
    stop = StopOnce(WorkerKilledError())
    parked = _park(stop)
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    for _ in range(REVIEW_ANSWER_JOB_ATTEMPTS):
        stop.armed = True
        with pytest.raises(WorkerKilledError):
            parked.run_job()
        parked.harness.clock.advance(LEASE + timedelta(seconds=1))

    assert parked.run_job() is False

    job = parked.jobs()[0]
    assert (job.status, job.attempts, job.last_error) == (
        JobStatus.FAILED,
        REVIEW_ANSWER_JOB_ATTEMPTS,
        STOPPED_UNEXPECTEDLY,
    )
    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.RESOLVED)
    reason = parked.harness.receipt(parked.receipt_id).failure_reason
    assert reason is FailureReason.STOPPED_UNEXPECTEDLY

    RetryReceipt(parked.harness.uow_factory)(parked.receipt_id)
    assert parked.run_job() is True

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert parked.harness.receipt(parked.receipt_id).store_name == "Costco"
    assert parked.pending() == []


def _workers_die(parked: Parked) -> None:
    """Every worker that takes the queued answer dies before it gives it: its attempts used up."""
    for _ in range(REVIEW_ANSWER_JOB_ATTEMPTS):
        with parked.harness.uow_factory() as uow:
            assert uow.jobs.claim("dead-worker", LEASE) is not None
            uow.commit()
        parked.harness.clock.advance(LEASE + timedelta(seconds=1))


def test_h4_04_try_again_before_the_answer_was_taken_gives_the_review_back_to_answer() -> None:
    parked = _park()
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    _workers_die(parked)
    assert parked.run_job() is False
    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.OPEN)

    RetryReceipt(parked.harness.uow_factory)(parked.receipt_id)
    assert parked.run_job() is True

    assert parked.state() == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
        ReviewStatus.OPEN,
    )
    assert parked.reader.calls == 1
    assert parked.pending() == [parked.review_id]
    with pytest.raises(ConflictError, match="not failed"):
        RetryReceipt(parked.harness.uow_factory)(parked.receipt_id)
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    assert parked.run_job() is True
    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("the receipt could not be failed"), WorkerKilledError()],
    ids=["recovery-raises", "worker-dies-mid-recovery"],
)
def test_h4_04_a_recovery_that_fails_changes_nothing_and_the_next_sweep_recovers(
    parked: Parked, failure: BaseException
) -> None:
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    _workers_die(parked)
    before = (parked.state(), parked.jobs())

    def broken(*_: object, **__: object) -> None:
        raise failure

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SqliteReceiptRepository, "update_status", broken)
        if isinstance(failure, Exception):
            assert parked.run_job() is False  # idle: the worker waits a poll, never spins
        else:
            with pytest.raises(WorkerKilledError):
                parked.run_job()
    after_failure = (parked.state(), parked.jobs())

    assert parked.run_job() is False

    assert after_failure == before
    job = parked.jobs()[0]
    assert (job.status, job.last_error) == (JobStatus.FAILED, STOPPED_UNEXPECTEDLY)
    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.OPEN)


def _answer_taken_then_workers_die(parked: Parked, stop: StopOnce, *, force: bool = False) -> None:
    """The graph takes the answer, then every worker dies before saving: the sweep fails the
    receipt as stopped unexpectedly and closes its review."""
    if force:
        parked.answer(parked.review_id, None, force=True, reason="  torn receipt ")
    else:
        parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    for _ in range(REVIEW_ANSWER_JOB_ATTEMPTS):
        stop.armed = True
        with pytest.raises(WorkerKilledError):
            parked.run_job()
        parked.harness.clock.advance(LEASE + timedelta(seconds=1))
    assert parked.run_job() is False
    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.RESOLVED)


def test_try_again_completing_a_forced_answer_remembers_why() -> None:
    stop = StopOnce(WorkerKilledError())
    parked = _park(stop)
    _answer_taken_then_workers_die(parked, stop, force=True)

    RetryReceipt(parked.harness.uow_factory)(parked.receipt_id)
    assert parked.run_job() is True

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert _corrections(parked) == ["torn receipt"]


def test_cli_resume_decides_as_try_again_does() -> None:
    """``process --resume`` of a FAILED receipt with an OPEN review: the thread that took the
    answer completes and closes it; one still waiting for it gives it back to the person."""
    stop = StopOnce(WorkerKilledError())
    taken = _park(stop)
    _answer_taken_then_workers_die(taken, stop)
    waiting = _park()
    waiting.answer(waiting.review_id, _corrected(), force=True, reason="checked the paper")
    _workers_die(waiting)
    assert waiting.run_job() is False

    resumed = taken.harness.resume(taken.receipt_id)
    with pytest.raises(ConflictError, match="waits for review"):
        waiting.harness.resume(waiting.receipt_id)

    assert resumed.graph is not None
    assert taken.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert waiting.state() == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
        ReviewStatus.OPEN,
    )
    assert waiting.pending() == [waiting.review_id]


def test_try_again_reading_the_photo_afresh_dismisses_the_stale_review(parked: Parked) -> None:
    parked.harness.runner.reset(parked.receipt_id)
    with parked.harness.uow_factory() as uow:
        uow.receipts.update_status(parked.receipt_id, ReceiptStatus.FAILED, ProcessingStage.FAILED)
        uow.commit()
    parked.reader.script(answer())

    RetryReceipt(parked.harness.uow_factory)(parked.receipt_id)
    assert parked.run_job() is True

    assert parked.state() == (
        ReceiptStatus.COMPLETED,
        ProcessingStage.DONE,
        ReviewStatus.DISMISSED,
    )
    with parked.harness.uow_factory(read_only=True) as uow:
        assert uow.reviews.find_open(parked.receipt_id) is None


def test_h4_04_a_stale_sweeper_changes_nothing_after_a_new_answer_is_queued(
    parked: Parked,
) -> None:
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    _workers_die(parked)
    with parked.harness.uow_factory(read_only=True) as uow:
        stale = uow.jobs.abandoned()
    assert parked.run_job() is False
    RetryReceipt(parked.harness.uow_factory)(parked.receipt_id)
    assert parked.run_job() is True
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    before = (parked.state(), parked.jobs())
    late = ProcessReceiptJobs(
        parked.harness.uow_factory,
        parked.harness.process,
        parked.harness.tracer,
        worker_id="late-sweeper",
        lease=LEASE,
        resolve_review=parked.harness.resolve,
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SqliteJobQueue, "abandoned", lambda _: stale)
        patch.setattr(SqliteJobQueue, "claim", lambda *_: None)
        assert late() is False

    assert before[0] == (ReceiptStatus.PROCESSING, ProcessingStage.QUEUED, ReviewStatus.OPEN)
    assert [job.status for job in before[1]] == [
        JobStatus.FAILED,
        JobStatus.SUCCEEDED,
        JobStatus.PENDING,
    ]
    assert (parked.state(), parked.jobs()) == before


def test_a_worker_that_died_after_the_run_finished_only_closes_the_review(parked: Parked) -> None:
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    parked.harness.runner.resume(parked.receipt_id, _corrected(), attribute_discounts=False)
    assert parked.harness.receipt(parked.receipt_id).status is ReceiptStatus.COMPLETED

    assert parked.run_job() is True

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert parked.jobs()[0].status is JobStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError("matching failed"), FailureReason.UNKNOWN),
        (ReaderUnavailableError("matching failed"), FailureReason.CLAUDE_UNAVAILABLE),
    ],
    ids=["unknown", "claude-unavailable"],
)
def test_a_run_that_fails_after_the_answer_was_saved_is_continued_like_any_failed_run(
    error: Exception, reason: FailureReason
) -> None:
    stop = StopOnce(error)
    parked = _park(stop)
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    stop.armed = True

    assert parked.run_job() is True

    assert parked.jobs()[0].last_error == "matching failed"
    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.RESOLVED)
    assert parked.harness.receipt(parked.receipt_id).failure_reason is reason
    resumed = parked.harness.resume(parked.receipt_id).graph
    assert resumed is not None
    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert parked.harness.receipt(parked.receipt_id).store_name == "Costco"


def test_a_second_answer_to_the_same_review_is_a_conflict(parked: Parked) -> None:
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")

    with pytest.raises(ConflictError, match="already answered"):
        parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")

    assert len(parked.jobs()) == 1
    parked.run_job()
    with pytest.raises(ConflictError, match="resolved, not open"):
        parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")


def test_an_answer_the_worker_cannot_apply_goes_back_to_a_person(parked: Parked) -> None:
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    parked.harness.runner.reset(parked.receipt_id)

    assert parked.run_job() is True

    (job,) = parked.jobs()
    assert (job.status, job.attempts) == (JobStatus.FAILED, 1)
    assert job.last_error == (
        f"nothing to resume: the agents' run of receipt {parked.receipt_id} does not exist"
    )
    assert parked.state() == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
        ReviewStatus.OPEN,
    )
    assert parked.pending() == [parked.review_id]


def test_a_review_resolved_meanwhile_fails_its_queued_answer_and_keeps_the_receipt(
    parked: Parked,
) -> None:
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    with parked.harness.uow_factory() as uow:
        uow.reviews.resolve(parked.review_id)
        uow.receipts.update_status(parked.receipt_id, ReceiptStatus.COMPLETED, ProcessingStage.DONE)
        uow.commit()

    assert parked.run_job() is True

    assert parked.jobs()[0].status is JobStatus.FAILED
    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)


def test_an_answer_taken_by_the_graph_whose_save_failed_is_not_answered_again() -> None:
    """The review node checkpointed the answer and ``persist`` failed before saving it (the
    receipt still QUEUED): the checkpoint says the answer was taken, so the review is closed
    and the receipt FAILED; a second answer is refused instead of being silently dropped by a
    resume of the old checkpoint, and ``process --resume`` saves the first one."""
    stop = StopOnce(RuntimeError("save failed"), tool="save_receipt")
    parked = _park(stop)
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    stop.armed = True

    assert parked.run_job() is True

    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.RESOLVED)
    with pytest.raises(ConflictError, match="not open"):
        parked.answer(
            parked.review_id,
            replace(_corrected(), store_name="Other"),
            force=True,
            reason="checked the paper",
        )
    assert len(parked.jobs()) == 1
    parked.harness.resume(parked.receipt_id)
    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert parked.harness.receipt(parked.receipt_id).store_name == "Costco"


def test_giving_back_a_failed_answer_never_demotes_a_receipt_completed_meanwhile(
    parked: Parked,
) -> None:
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    with parked.harness.uow_factory() as uow:
        uow.receipts.update_status(parked.receipt_id, ReceiptStatus.COMPLETED, ProcessingStage.DONE)
        uow.commit()

    parked.harness.resolve.give_back(parked.review_id, forced_reason=None)

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.OPEN)


def test_an_open_review_of_a_failed_receipt_is_not_answerable_until_try_again(
    parked: Parked,
) -> None:
    """Its graph may already have taken an earlier answer, which a new one would silently
    replace: the person retries the receipt instead."""
    with parked.harness.uow_factory() as uow:
        uow.receipts.update_status(parked.receipt_id, ReceiptStatus.FAILED, ProcessingStage.FAILED)
        uow.commit()
    before = (parked.state(), parked.jobs())

    with pytest.raises(ConflictError, match="use Try again on the receipt"):
        parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")

    assert parked.pending() == []
    assert (parked.state(), parked.jobs()) == before
    RetryReceipt(parked.harness.uow_factory)(parked.receipt_id)
    assert parked.run_job() is True
    assert parked.pending() == [parked.review_id]
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    assert parked.run_job() is True
    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)


def test_an_open_review_beside_a_saved_receipt_is_closed_and_the_answer_refused(
    parked: Parked,
) -> None:
    """The run finished but closing the review did not: answering it must not demote the
    COMPLETED receipt to PROCESSING with an answer the worker would discard."""
    parked.harness.runner.resume(parked.receipt_id, _corrected(), attribute_discounts=False)
    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.OPEN)
    assert parked.pending() == []

    with pytest.raises(ConflictError, match="already saved"):
        parked.answer(
            parked.review_id,
            replace(_corrected(), store_name="Other"),
            force=True,
            reason="checked the paper",
        )

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert parked.jobs() == []


# --- review gaps: accept as read, currency, the crash gap, imported reviews, links -----------


def _all_reviews(harness: Harness) -> list[ReviewItem]:
    found: list[ReviewItem] = []
    with harness.uow_factory(read_only=True) as uow:
        while (review := uow.reviews.get(len(found) + 1)) is not None:
            found.append(review)
    return found


def _assert_reviews_consistent(harness: Harness) -> None:
    """C-08: at most one OPEN review per receipt, and a NEEDS_REVIEW receipt has one."""
    reviews = _all_reviews(harness)
    for receipt_id in {review.receipt_id for review in reviews}:
        status = harness.receipt(receipt_id).status
        statuses = [review.status for review in reviews if review.receipt_id == receipt_id]
        assert statuses.count(ReviewStatus.OPEN) <= 1
        if status is ReceiptStatus.NEEDS_REVIEW:
            assert ReviewStatus.OPEN in statuses


def _corrections(parked: Parked) -> list[str | None]:
    corrections = parked.database.state.corrections.values()
    return [
        correction.new_value
        for correction in corrections
        if correction.receipt_id == parked.receipt_id
    ]


def test_a1_09_a_forced_accept_of_a_reading_with_issues_is_queued_and_saved() -> None:
    parked = _park(reading=codex_answer())

    with pytest.raises(InconsistentDraftError):
        parked.answer(parked.review_id, None)
    parked.answer(parked.review_id, None, force=True, reason=f"per pack {TEST_PAN}")
    (job,) = parked.jobs()
    assert parked.run_job() is True

    assert job.payload is not None
    assert job.payload["force"] is True
    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert _corrections(parked) == [f"per pack {MASKED_TEST_PAN}"]


def _in_currency(draft: ReceiptDraft, currency: str) -> ReceiptDraft:
    def money(amount: Money | None) -> Money | None:
        return None if amount is None else Money(amount.amount_cents, currency)

    return replace(
        draft,
        currency=currency,
        subtotal=money(draft.subtotal),
        tax=money(draft.tax),
        total=Money(draft.total.amount_cents, currency),
        items=tuple(
            replace(
                line,
                unit_price=money(line.unit_price),
                line_total=Money(line.line_total.amount_cents, currency),
            )
            for line in draft.items
        ),
    )


def test_a2_06_the_answer_s_currency_reaches_the_saved_receipt(parked: Parked) -> None:
    parked.answer(
        parked.review_id, _in_currency(_corrected(), "USD"), force=True, reason="checked the paper"
    )
    parked.run_job()

    receipt = parked.harness.receipt(parked.receipt_id)
    assert receipt.status is ReceiptStatus.COMPLETED
    amounts = [receipt.subtotal, receipt.tax, receipt.total]
    amounts += [line.line_total for line in receipt.items]
    assert receipt.currency == "USD"
    assert {amount.currency for amount in amounts if amount is not None} == {"USD"}
    assert receipt.total == Money(1635, "USD")


def _gap(stop: StopOnce | None = None) -> Parked:
    """F-GAP: the supervisor opened the review (receipt NEEDS_REVIEW), then the worker died
    before the supervisor's checkpoint; ``stop``, armed later, fails a later tool call."""

    def crash(gateway: ToolGateway) -> ToolGateway:
        return CrashAfterToolGateway(gateway, "open_review", WorkerKilledError())

    harness, database = _harness(
        ScriptedReader(inconsistent_answer()),
        crash if stop is None else lambda gateway: stop.wrap(crash(gateway)),
    )
    with pytest.raises(WorkerKilledError):
        harness.process_image()
    (review,) = harness.open_reviews()
    gap = _parked_at(harness, database, review.receipt_id, require_id(review.id))
    assert gap.state() == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
        ReviewStatus.OPEN,
    )
    assert not harness.runner.waiting_for_review(gap.receipt_id)
    assert not harness.runner.answer_taken(gap.receipt_id)
    return gap


def test_c01_continuing_a_run_stopped_in_the_gap_lands_on_the_same_review() -> None:
    gap = _gap()

    outcome = gap.harness.resume(gap.receipt_id).graph

    assert outcome is not None
    assert (outcome.status, outcome.review_id) == (ReceiptStatus.NEEDS_REVIEW, gap.review_id)
    assert [review.id for review in gap.harness.open_reviews()] == [gap.review_id]
    assert gap.state() == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
        ReviewStatus.OPEN,
    )
    pending = gap.harness.runner.pending_draft(gap.receipt_id)
    assert pending is not None
    assert pending.total == cad(2683)
    assert gap.reader.calls == 1


def test_c02_a_corrected_draft_answered_in_the_gap_is_applied() -> None:
    gap = _gap()
    assert gap.pending() == [gap.review_id]

    gap.answer(gap.review_id, _corrected(), force=True, reason="checked the paper")
    assert gap.run_job() is True

    assert gap.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    receipt = gap.harness.receipt(gap.receipt_id)
    assert (receipt.store_name, receipt.total) == ("Costco", cad(1635))
    assert [line.raw_text for line in receipt.items] == ["KS ORG EGGS 24CT", "KS 2% MILK 4L"]
    assert gap.jobs()[0].status is JobStatus.SUCCEEDED
    assert gap.reader.calls == 1
    _assert_reviews_consistent(gap.harness)


def test_c03_c07_in_the_gap_the_reading_is_shown_and_accepting_it_saves_it() -> None:
    """The review waits on the reading its stopped run holds: it is listed and shown with it,
    accepting needs force and a reason, and the worker continues the run to the review and
    saves the reading."""
    gap = _gap()

    shown = GetPendingReview(gap.harness.uow_factory, gap.harness.runner)(gap.review_id)
    with pytest.raises(InconsistentDraftError):
        gap.answer(gap.review_id, None)
    gap.answer(gap.review_id, None, force=True, reason="checked the paper")
    assert gap.run_job() is True

    assert shown.draft is not None
    assert shown.draft.total == cad(2683)
    assert gap.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert gap.harness.receipt(gap.receipt_id).total == cad(2683)
    assert _corrections(gap) == ["checked the paper"]
    _assert_reviews_consistent(gap.harness)


def test_c06_a_failure_before_the_answer_is_taken_gives_the_review_back() -> None:
    """The worker's run to the review fails (open_review raises): the answer was not taken,
    so the review is answerable again and no reason is remembered; the next answer is
    saved with exactly one."""
    stop = StopOnce(RuntimeError("the review could not be opened"), tool="open_review")
    gap = _gap(stop)
    gap.answer(gap.review_id, _corrected(), force=True, reason="first")
    stop.armed = True

    assert gap.run_job() is True

    assert gap.jobs()[0].status is JobStatus.FAILED
    assert gap.state() == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
        ReviewStatus.OPEN,
    )
    assert _corrections(gap) == []
    _assert_reviews_consistent(gap.harness)
    wrong = replace(_corrected(), total=cad(1600))
    gap.answer(gap.review_id, wrong, force=True, reason="second")
    assert gap.run_job() is True
    assert gap.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert _corrections(gap) == ["second"]


def test_c06_a_failure_after_the_answer_is_taken_keeps_it_and_resume_saves_it() -> None:
    """Once the graph took the answer the review is closed (never reopened for a second
    answer the checkpoint would ignore), the reason kept, and ``process --resume`` saves the
    answer that was given."""
    stop = StopOnce(RuntimeError("save failed"), tool="save_receipt")
    gap = _gap(stop)
    wrong = replace(_corrected(), total=cad(1600))
    gap.answer(gap.review_id, wrong, force=True, reason="torn")
    stop.armed = True

    assert gap.run_job() is True

    assert gap.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.RESOLVED)
    assert _corrections(gap) == ["torn"]
    with pytest.raises(ConflictError, match="not open"):
        gap.answer(gap.review_id, _corrected(), force=True, reason="checked the paper")
    gap.harness.resume(gap.receipt_id)
    assert gap.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert gap.harness.receipt(gap.receipt_id).total == cad(1600)
    assert _corrections(gap) == ["torn"]
    _assert_reviews_consistent(gap.harness)


# --- imported reviews (no thread) ------------------------------------------------------------


def _imported(**overrides: Any) -> Parked:
    """F-IMPORT: ``receipts import`` of a draft whose milk line is 6.85 (it does not add up);
    its eggs and milk lines name products."""
    harness, database = _harness(ScriptedReader(), None)
    eggs, milk = receipt_draft().items
    draft = receipt_draft(
        items=(eggs, replace(milk, line_total=cad(685))),
        product_names={0: "Eggs", 1: "Milk"},
        **overrides,
    )
    recorded = harness.record(draft)
    assert recorded.review is not None
    assert recorded.review.thread_id is None
    return _parked_at(
        harness, database, require_id(recorded.receipt.id), require_id(recorded.review.id)
    )


def test_i01_an_imported_review_is_listed_with_the_receipt_as_stored() -> None:
    imported = _imported()

    (listed,) = ListPendingReviews(imported.harness.uow_factory, imported.harness.runner)()

    stored = imported.harness.receipt(imported.receipt_id)
    assert listed.review.id == imported.review_id
    assert listed.draft is not None
    assert (
        listed.draft.store_name,
        listed.draft.purchased_on,
        listed.draft.currency,
        listed.draft.subtotal,
        listed.draft.tax,
        listed.draft.total,
    ) == (
        stored.store_name,
        stored.purchased_on,
        stored.currency,
        stored.subtotal,
        stored.tax,
        stored.total,
    )
    assert listed.draft.items == tuple(replace(line, id=None) for line in stored.items)


def test_i03_i08_a_corrected_draft_is_saved_directly_as_a_clean_import_would() -> None:
    imported = _imported()
    products = [line.product_id for line in imported.harness.receipt(imported.receipt_id).items]
    eggs, milk = receipt_draft().items
    corrected = receipt_draft(
        items=(
            eggs,
            replace(milk, raw_text="KS 1% MILK 4L"),
            line_item(
                position=2,
                raw_text=f"GUM {TEST_PAN}",
                unit_price=cad(100),
                line_total=cad(100),
            ),
        ),
        subtotal=cad(1657),
        total=cad(1735),
    )

    saved = imported.answer(imported.review_id, corrected, force=True, reason=f"card {TEST_PAN}")

    assert saved.status is ReceiptStatus.COMPLETED
    assert imported.state() == (
        ReceiptStatus.COMPLETED,
        ProcessingStage.DONE,
        ReviewStatus.RESOLVED,
    )
    stored = imported.harness.receipt(imported.receipt_id)
    assert [(line.raw_text, line.line_total, line.product_id) for line in stored.items] == [
        ("KS ORG EGGS 24CT", cad(899), products[0]),
        ("KS 1% MILK 4L", cad(658), None),
        (f"GUM {MASKED_TEST_PAN}", cad(100), None),
    ]
    eggs_id, milk_id = products
    assert eggs_id is not None
    assert milk_id is not None
    (observation,) = imported.harness.price_history(eggs_id)
    assert (observation.receipt_id, observation.observed_on) == (
        imported.receipt_id,
        PURCHASED_ON,
    )
    assert imported.harness.price_history(milk_id) == ()
    assert _corrections(imported) == [f"card {MASKED_TEST_PAN}"]
    assert imported.jobs() == []
    assert not imported.harness.runner.has_thread(imported.receipt_id)
    assert imported.reader.calls == 0
    assert card_numbers(repr(stored)) == []
    with pytest.raises(ConflictError, match="resolved, not open"):
        imported.answer(imported.review_id, corrected, force=True, reason="checked the paper")


def test_i06_two_answers_racing_to_an_imported_review_save_only_the_first() -> None:
    """Both answers pass the check; the one that commits first wins, the other is refused in
    its own write transaction, and only the winner's contents and reason are kept."""
    imported = _imported()
    resolve = imported.harness.resolve
    first = resolve.check(
        imported.review_id, _corrected(), force=True, reason="first", attribute=False
    )
    wrong = replace(_corrected(), total=cad(1600))
    second = resolve.check(imported.review_id, wrong, force=True, reason="late", attribute=False)

    resolve.save_directly(first, "first")
    with pytest.raises(ConflictError, match="no longer open"):
        resolve.save_directly(second, "late")

    stored = imported.harness.receipt(imported.receipt_id)
    assert (stored.store_name, stored.total) == ("Costco", cad(1635))
    assert _corrections(imported) == ["first"]


def test_i06_an_answer_checked_before_another_was_queued_is_refused_when_it_writes(
    parked: Parked,
) -> None:
    """The late answer is checked while the review is open, then another one is queued
    before it writes: it is refused in its own write transaction, and only the first is
    saved."""
    original = parked.harness.resolve.check
    raced: list[bool] = []

    def check_then_lose_the_race(*args: Any, **kwargs: Any) -> Any:
        checked = original(*args, **kwargs)
        if not raced:
            raced.append(True)
            parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
        return checked

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(parked.harness.resolve, "check", check_then_lose_the_race)
        with pytest.raises(ConflictError, match="already answered"):
            parked.answer(
                parked.review_id,
                replace(_corrected(), store_name="Late"),
                force=True,
                reason="checked the paper",
            )

    assert len(parked.jobs()) == 1
    assert parked.run_job() is True
    assert parked.harness.receipt(parked.receipt_id).store_name == "Costco"
    assert [job.status for job in parked.jobs()] == [JobStatus.SUCCEEDED]


def test_i07_a_failed_direct_save_writes_nothing_and_can_be_answered_again() -> None:
    """The forced answer names a new product and keeps a reason: both are written before the
    review is closed, and both are rolled back with everything else when closing it fails."""
    imported = _imported()
    before = imported.database.state
    corrected = replace(_corrected(), total=cad(1600), product_names={0: "Fresh Eggs"})

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("the review could not be closed")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SqliteReviewQueue, "resolve", broken)
        with pytest.raises(RuntimeError):
            imported.answer(imported.review_id, corrected, force=True, reason="why")

    assert imported.database.state == before
    assert imported.state() == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
        ReviewStatus.OPEN,
    )
    imported.answer(imported.review_id, corrected, force=True, reason="why")
    assert imported.state() == (
        ReceiptStatus.COMPLETED,
        ProcessingStage.DONE,
        ReviewStatus.RESOLVED,
    )
    after = imported.database.state
    assert len(after.products) == len(before.products) + 1
    assert _corrections(imported) == ["why"]
    _assert_reviews_consistent(imported.harness)


def test_k01_a_receipt_names_its_open_review_only_while_it_waits_for_it(parked: Parked) -> None:
    find = FindOpenReview(parked.harness.uow_factory)

    waiting = find(parked.harness.receipt(parked.receipt_id))
    parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    queued = find(parked.harness.receipt(parked.receipt_id))
    parked.run_job()
    saved = find(parked.harness.receipt(parked.receipt_id))
    parked.harness.runner.reset(parked.receipt_id)
    with parked.harness.uow_factory() as uow:
        uow.receipts.update_status(
            parked.receipt_id, ReceiptStatus.NEEDS_REVIEW, ProcessingStage.NEEDS_REVIEW
        )
        uow.commit()

    assert (waiting, queued, saved) == (parked.review_id, None, None)
    assert find(parked.harness.receipt(parked.receipt_id)) is None


# --- review round 1: one rule for answers, consumed answers, exhausted jobs ------------------


SAVED_UNCHANGED = {
    "unreadable-unit-price": receipt_answer(
        [item("PEPPERS", "8.00", quantity="2", unit_price="two for eight")], total="8.00"
    ),
    "no-date": answer(purchased_on=None),
    "item-price-math": receipt_answer(
        [item("SOAP", "8.00", regular_price="10.00", discount="1.00")],
        subtotal="9.00",
        total="9.00",
    ),
    "negative-item": receipt_answer(
        [item("SOAP", "-1.00", regular_price="1.00", discount="2.00"), item("X", "5.00")],
        total="4.00",
    ),
    "deal-rounding": receipt_answer(
        [item("YOG", "19.98", quantity="6", unit_price="3/$10.00")], total="19.98"
    ),
    "unit-price": codex_answer(),
    "no-store": answer(store_name=""),
}


def _first_line(draft: ReceiptDraft, **changes: Any) -> ReceiptDraft:
    first, *rest = draft.items
    return replace(draft, items=(replace(first, **changes), *rest))


type Edit = Callable[[ReceiptDraft], ReceiptDraft]

ANSWERS: dict[str, tuple[dict[str, Any], Edit | None]] = {
    **{
        f"{name}-unchanged": (reading, lambda draft: draft)
        for name, reading in SAVED_UNCHANGED.items()
    },
    "accept": (codex_answer(), None),
    "tax-blank-to-0": (
        SAVED_UNCHANGED["deal-rounding"],
        lambda draft: replace(draft, tax=Money(0, draft.currency)),
    ),
    "store-dot": (codex_answer(), lambda draft: replace(draft, store_name=f"{draft.store_name}.")),
    "description": (codex_answer(), lambda draft: _first_line(draft, description="Sourdough")),
    "category": (codex_answer(), lambda draft: _first_line(draft, category=Category.BAKERY)),
    "unit": (codex_answer(), lambda draft: _first_line(draft, unit=Unit.KG)),
    "line-added": (
        codex_answer(),
        lambda draft: replace(
            draft,
            items=(
                *draft.items,
                line_item(position=1, raw_text="GUM", unit_price=None, line_total=cad(0)),
            ),
        ),
    ),
    "line-removed": (
        inconsistent_answer(),
        lambda draft: replace(
            draft,
            items=draft.items[:2],
            subtotal=cad(1557),
            tax=cad(78),
            total=cad(1635),
        ),
    ),
    "currency": (codex_answer(), lambda draft: _in_currency(draft, "USD")),
    "unit-price-fixed": (codex_answer(), lambda draft: _first_line(draft, unit_price=cad(400))),
    "date-added": (
        SAVED_UNCHANGED["no-date"],
        lambda draft: replace(draft, purchased_on=PURCHASED_ON),
    ),
}


@pytest.mark.parametrize(("reading", "edit"), list(ANSWERS.values()), ids=list(ANSWERS))
def test_always_ask_every_answer_to_a_review_with_issues_needs_a_reason(
    reading: dict[str, Any], edit: Edit | None
) -> None:
    """Accepted, saved as shown, edited cosmetically, by structure, by currency or fixed: an
    answer to a review opened with issues is refused without force and a reason, and kept
    with them, the reason stored with card numbers masked."""
    parked = _park(reading=reading)
    shown = GetPendingReview(parked.harness.uow_factory, parked.harness.runner)(parked.review_id)
    assert shown.draft is not None
    given = None if edit is None else edit(shown.draft)

    with pytest.raises(InconsistentDraftError):
        parked.answer(parked.review_id, given)
    assert parked.jobs() == []
    parked.answer(parked.review_id, given, force=True, reason=f"paid with {TEST_PAN}")
    assert parked.run_job() is True

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert _corrections(parked) == [f"paid with {MASKED_TEST_PAN}"]


def test_r3_a_review_whose_answer_the_graph_took_takes_no_other() -> None:
    """The graph took an answer and its run failed before saving it (the review still OPEN
    beside a NEEDS_REVIEW receipt): another answer would be ignored, so it is refused."""
    stop = StopOnce(RuntimeError("save failed"), tool="save_receipt")
    parked = _park(stop)
    stop.armed = True
    with pytest.raises(RuntimeError):
        parked.harness.runner.resume(parked.receipt_id, _corrected(), attribute_discounts=False)
    assert parked.state() == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
        ReviewStatus.OPEN,
    )

    with pytest.raises(ConflictError, match="already being saved"):
        parked.answer(
            parked.review_id,
            replace(_corrected(), store_name="Other"),
            force=True,
            reason="checked the paper",
        )

    assert parked.jobs() == []


def test_r3_review_resolve_whose_run_fails_after_taking_the_answer_closes_the_review() -> None:
    """The CLI settles a failed answer as the worker does: the answer was taken, so the
    receipt is FAILED and the review closed with the reason; a web answer is then refused and
    ``process --resume`` saves the first answer."""
    stop = StopOnce(RuntimeError("save failed"), tool="save_receipt")
    parked = _park(stop)
    stop.armed = True
    wrong = replace(_corrected(), store_name="CLI answer", total=cad(1600))

    with pytest.raises(RuntimeError):
        parked.harness.resolve(parked.review_id, wrong, force=True, reason="torn")

    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.RESOLVED)
    assert _corrections(parked) == ["torn"]
    with pytest.raises(ConflictError, match="not open"):
        parked.answer(parked.review_id, _corrected(), force=True, reason="checked the paper")
    parked.harness.resume(parked.receipt_id)
    assert parked.harness.receipt(parked.receipt_id).store_name == "CLI answer"


def test_r5_a_job_whose_workers_die_after_the_gap_s_answer_was_taken_closes_the_review() -> None:
    """From the crash gap: each worker continues the run to the review (which sets the receipt
    NEEDS_REVIEW again), gives the answer and dies before saving it. The exhausted job closes
    the review and fails the receipt, so a second answer is refused rather than taken and
    ignored; Try again saves the first answer."""
    stop = StopOnce(WorkerKilledError(), tool="save_receipt")
    gap = _gap(stop)
    wrong = replace(_corrected(), store_name="First answer", total=cad(1600))
    gap.answer(gap.review_id, wrong, force=True, reason="torn")
    for _ in range(REVIEW_ANSWER_JOB_ATTEMPTS):
        stop.armed = True
        with pytest.raises(WorkerKilledError):
            gap.run_job()
        gap.harness.clock.advance(LEASE + timedelta(seconds=1))
    assert gap.harness.receipt(gap.receipt_id).status is ReceiptStatus.NEEDS_REVIEW

    assert gap.run_job() is False

    assert gap.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.RESOLVED)
    assert _corrections(gap) == ["torn"]
    with pytest.raises(ConflictError):
        gap.answer(gap.review_id, _corrected(), force=True, reason="checked the paper")
    RetryReceipt(gap.harness.uow_factory)(gap.receipt_id)
    assert gap.run_job() is True
    assert gap.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert gap.harness.receipt(gap.receipt_id).store_name == "First answer"
    assert _corrections(gap) == ["torn"]


def test_m11_a_forced_answer_given_before_the_worker_died_keeps_its_reason() -> None:
    stop = StopOnce(WorkerKilledError())
    parked = _park(stop)
    parked.answer(parked.review_id, None, force=True, reason="torn")
    stop.armed = True
    with pytest.raises(WorkerKilledError):
        parked.run_job()
    parked.harness.clock.advance(LEASE + timedelta(seconds=1))

    assert parked.run_job() is True

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert _corrections(parked) == ["torn"]


def test_m1_an_imported_review_whose_receipt_no_longer_waits_is_not_saved() -> None:
    imported = _imported()
    with imported.harness.uow_factory() as uow:
        uow.receipts.update_status(
            imported.receipt_id, ReceiptStatus.FAILED, ProcessingStage.FAILED
        )
        uow.commit()
    before = imported.database.state

    with pytest.raises(ConflictError, match="not waiting"):
        imported.harness.resolve(
            imported.review_id, _corrected(), force=True, reason="checked the paper"
        )

    after = imported.database.state
    assert replace(after, jobs={}) == replace(before, jobs={})


def test_m2_accepting_an_imported_review_keeps_its_discounts_where_the_import_put_them() -> None:
    """The discount rule links TPD/1234567 to the eggs (code 1234567); the stored receipt
    keeps it at receipt level, and accepting as read keeps it there."""
    harness, database = _harness(ScriptedReader(), None)
    draft = simple_draft(link=False, total=cad(9999))
    recorded = harness.record(draft)
    assert recorded.review is not None
    assert recorded.receipt.items[-1].applies_to_position == 0
    unlinked = harness.record.replace(require_id(recorded.receipt.id), draft, attribute=False)
    before = [line.applies_to_position for line in unlinked.receipt.items]
    assert before[-1] is None
    imported = _parked_at(
        harness, database, require_id(recorded.receipt.id), require_id(recorded.review.id)
    )

    imported.harness.resolve(imported.review_id, force=True, reason="total smudged")

    lines = imported.harness.receipt(imported.receipt_id).items
    assert [line.applies_to_position for line in lines] == before
    assert imported.state() == (
        ReceiptStatus.COMPLETED,
        ProcessingStage.DONE,
        ReviewStatus.RESOLVED,
    )


def test_a_review_whose_issues_this_version_cannot_read_still_asks_why() -> None:
    """Issues stored in an older shape are not read (no 500): an answer is judged on its own
    checks, and, the review having been opened with issues, still needs a reason."""
    imported = _imported()
    with imported.harness.uow_factory() as uow:
        uow.reviews.dismiss(imported.review_id)
        review = uow.reviews.open(
            imported.receipt_id,
            ReviewReason.VALIDATION_FAILED,
            {"issues": [{"kind": "subtotal", "text": "an older shape"}]},
            thread_id=None,
        )
        uow.commit()
    review_id = require_id(review.id)

    with pytest.raises(InconsistentDraftError) as raised:
        imported.harness.resolve(review_id)
    with pytest.raises(DomainValidationError, match="opened with issues"):
        imported.harness.resolve(review_id, _corrected())
    saved = imported.harness.resolve(review_id, _corrected(), force=True, reason="checked")

    assert IssueCode.LINE_MATH in {issue.code for issue in raised.value.report.issues}
    assert saved.status is ReceiptStatus.COMPLETED
    assert _corrections(imported) == ["checked"]


def test_review_resolve_failing_after_the_answer_was_taken_closes_the_review_whatever_the_error(
    parked: Parked,
) -> None:
    """A run that fails after taking the answer gives the review back even when the failure
    is a domain error (a product gone before ``save_matches``): only a refused answer is
    left as it was."""
    stop = StopOnce(NotFoundError("product 9 does not exist"))
    gapless = _park(stop)
    stop.armed = True

    with pytest.raises(NotFoundError):
        gapless.harness.resolve(
            gapless.review_id, _corrected(), force=True, reason="checked the paper"
        )

    assert gapless.state() == (
        ReceiptStatus.FAILED,
        ProcessingStage.FAILED,
        ReviewStatus.RESOLVED,
    )
    with parked.harness.uow_factory() as uow:
        uow.receipts.update_status(parked.receipt_id, ReceiptStatus.FAILED, ProcessingStage.FAILED)
        uow.commit()
    with pytest.raises(InconsistentDraftError):
        parked.harness.resolve(parked.review_id)
    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.OPEN)


def test_a_killed_review_resolve_whose_hold_expires_closes_the_review_it_answered() -> None:
    """``review resolve`` gave the answer and the CLI was killed before saving it: its hold
    expires, and the sweep closes the review and fails the receipt, as for a worker's job."""
    stop = StopOnce(RuntimeError("save failed"), tool="save_receipt")
    parked = _park(stop)
    stop.armed = True
    with pytest.raises(RuntimeError):
        parked.harness.runner.resume(parked.receipt_id, _corrected(), attribute_discounts=False)
    hold = receipt_hold(parked.harness.uow_factory)
    payload = {"review_id": parked.review_id, "draft": None, "force": True, "reason": "torn"}
    with parked.harness.uow_factory() as uow:
        hold.take(uow, parked.receipt_id, payload=payload)
        uow.commit()
    parked.harness.clock.advance(timedelta(hours=1))

    assert parked.run_job() is False

    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.RESOLVED)
    assert _corrections(parked) == ["torn"]
    parked.harness.resume(parked.receipt_id)
    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)


# --- cold review: dates, interrupted answers, completed receipts, bounded recovery -----------


@pytest.mark.parametrize("make", [_park, _imported], ids=["scan", "import"])
def test_a_purchase_date_after_today_is_never_answered_even_forced(
    make: Callable[[], Parked],
) -> None:
    """Today is 2026-11-06 here."""
    parked = make()

    with pytest.raises(DomainValidationError, match="after today"):
        parked.answer(
            parked.review_id,
            replace(_corrected(), purchased_on=date(2026, 11, 7)),
            force=True,
            reason="checked",
        )

    assert parked.jobs() == []


@pytest.mark.parametrize(
    ("make", "codes"),
    [(_park, {"missing_date"}), (_imported, {"line_math", "items_vs_subtotal"})],
    ids=["scan", "import"],
)
def test_an_old_purchase_date_is_incomplete_only_for_a_scanned_receipt(
    make: Callable[[], Parked], codes: set[str]
) -> None:
    """An imported receipt over a year old is fine, as its clean import is; refused, the
    review's own issues are listed instead."""
    parked = make()

    with pytest.raises(InconsistentDraftError) as raised:
        parked.answer(parked.review_id, replace(_corrected(), purchased_on=date(2024, 1, 15)))

    assert {issue.code.value for issue in raised.value.report.issues} == codes


def test_an_interrupted_review_resolve_gives_the_review_back_with_its_reason(
    parked: Parked,
) -> None:
    """Ctrl-C after the graph took the answer: the CLI closes the review with the reason and
    fails the receipt, instead of leaving an answer nobody can give again."""
    stop = StopOnce(KeyboardInterrupt(), tool="save_receipt")
    interrupted = _park(stop)
    stop.armed = True

    with pytest.raises(KeyboardInterrupt):
        interrupted.harness.resolve(interrupted.review_id, _corrected(), force=True, reason="torn")

    assert interrupted.state() == (
        ReceiptStatus.FAILED,
        ProcessingStage.FAILED,
        ReviewStatus.RESOLVED,
    )
    assert _corrections(interrupted) == ["torn"]
    (hold,) = interrupted.jobs()
    assert hold.payload is not None
    assert (hold.payload["force"], hold.payload["reason"]) == (True, "torn")
    assert parked.state()[2] is ReviewStatus.OPEN


def test_giving_back_an_answer_taken_by_a_run_that_finished_closes_the_review_with_its_reason(
    parked: Parked,
) -> None:
    parked.harness.runner.resume(parked.receipt_id, _corrected(), attribute_discounts=False)
    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.OPEN)

    parked.harness.resolve.give_back(parked.review_id, forced_reason="torn")

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert _corrections(parked) == ["torn"]


def test_answering_never_starts_a_claude_read_to_reach_the_review() -> None:
    """A thread stopped before a supervisor that would read again (attempt 1 of 2) with an
    OPEN review of that thread: the answer runs the supervisor only, then is refused."""
    harness, database = _harness(ScriptedReader(inconsistent_answer()), None)
    harness = build_harness(
        ScriptedReader(inconsistent_answer()),
        settings=agent_settings(max_attempts=2),
        clock=harness.clock,
        uow_factory=database.uow_factory,
    )
    outcome = harness.process_image(interrupt_after="validate").graph
    assert outcome is not None
    receipt_id = outcome.receipt_id
    with harness.uow_factory() as uow:
        review = uow.reviews.open(
            receipt_id,
            ReviewReason.VALIDATION_FAILED,
            {"issues": []},
            thread_id=f"receipt-{receipt_id}",
        )
        uow.receipts.update_status(
            receipt_id, ReceiptStatus.NEEDS_REVIEW, ProcessingStage.NEEDS_REVIEW
        )
        uow.commit()

    with pytest.raises(ConflictError, match="not waiting"):
        harness.resolve(require_id(review.id), _corrected(), force=True, reason="checked")

    assert isinstance(harness.reader, ScriptedReader)
    assert harness.reader.calls == 1
