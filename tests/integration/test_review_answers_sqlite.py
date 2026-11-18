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
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta

import pytest

from paper_trail.adapters.sqlite.job_queue import SqliteJobQueue
from paper_trail.adapters.sqlite.receipt_repository import SqliteReceiptRepository
from paper_trail.application.jobs import STOPPED_UNEXPECTEDLY, ProcessReceiptJobs
from paper_trail.application.process_receipt import RetryReceipt
from paper_trail.application.receipts import (
    REVIEW_ANSWER_JOB_ATTEMPTS,
    AnswerReview,
    InconsistentDraftError,
    ListPendingReviews,
)
from paper_trail.domain.enums import (
    AgentRole,
    FailureReason,
    JobKind,
    JobStatus,
    ProcessingStage,
    ReceiptStatus,
    ReviewStatus,
)
from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import Job, JsonValue, ReceiptDraft
from paper_trail.ports.reader import ReaderUnavailableError
from paper_trail.ports.tools import ToolDescription, ToolGateway
from tests.agent_harness import Harness, agent_settings, build_harness
from tests.extractions import answer, inconsistent_answer
from tests.factories import MASKED_TEST_PAN, TEST_PAN, cad, receipt_draft, simple_draft
from tests.fakes.reader import ScriptedReader
from tests.pans import card_numbers
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


def _park(stop: StopOnce | None = None) -> Parked:
    """Review of a receipt whose one reading came back 9 cents short (VALIDATION_FAILED)."""
    harness = build_harness(
        ScriptedReader(inconsistent_answer()),
        settings=agent_settings(max_attempts=1),
        wrap_tools=None if stop is None else stop.wrap,
    )
    graph = harness.process_image().graph
    assert graph is not None
    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    return Parked(
        harness=harness,
        receipt_id=graph.receipt_id,
        review_id=require_id(graph.review_id),
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

    queued = parked.answer(parked.review_id, _corrected())

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
        parked.review_id, simple_draft(ocr_text=None, image_path=None, store_name="Costco")
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
    parked.answer(parked.review_id, _corrected())
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
    leaves the review OPEN; Try again resumes the thread, which completes and closes it."""
    stop = StopOnce(WorkerKilledError())
    parked = _park(stop)
    parked.answer(parked.review_id, _corrected())
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
    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.OPEN)
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
    parked.answer(parked.review_id, _corrected())
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
    parked.answer(parked.review_id, _corrected())
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
    parked.answer(parked.review_id, _corrected())
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
    receipt as stopped unexpectedly, its review left OPEN."""
    if force:
        parked.answer(parked.review_id, None, force=True, reason="  torn receipt ")
    else:
        parked.answer(parked.review_id, _corrected())
    for _ in range(REVIEW_ANSWER_JOB_ATTEMPTS):
        stop.armed = True
        with pytest.raises(WorkerKilledError):
            parked.run_job()
        parked.harness.clock.advance(LEASE + timedelta(seconds=1))
    assert parked.run_job() is False
    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.OPEN)


def test_try_again_completing_a_forced_answer_remembers_why() -> None:
    stop = StopOnce(WorkerKilledError())
    parked = _park(stop)
    _answer_taken_then_workers_die(parked, stop, force=True)

    RetryReceipt(parked.harness.uow_factory)(parked.receipt_id)
    assert parked.run_job() is True

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    correction = parked.harness.latest_correction(f"receipt:{parked.receipt_id}.arithmetic")
    assert correction is not None
    assert correction.new_value == "torn receipt"


def test_cli_resume_decides_as_try_again_does() -> None:
    """``process --resume`` of a FAILED receipt with an OPEN review: the thread that took the
    answer completes and closes it; one still waiting for it gives it back to the person."""
    stop = StopOnce(WorkerKilledError())
    taken = _park(stop)
    _answer_taken_then_workers_die(taken, stop)
    waiting = _park()
    waiting.answer(waiting.review_id, _corrected())
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
    parked.answer(parked.review_id, _corrected())
    _workers_die(parked)
    with parked.harness.uow_factory(read_only=True) as uow:
        stale = uow.jobs.abandoned()
    assert parked.run_job() is False
    RetryReceipt(parked.harness.uow_factory)(parked.receipt_id)
    assert parked.run_job() is True
    parked.answer(parked.review_id, _corrected())
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
    parked.answer(parked.review_id, _corrected())
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
    parked.answer(parked.review_id, _corrected())
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
    parked.answer(parked.review_id, _corrected())

    with pytest.raises(ConflictError, match="already answered"):
        parked.answer(parked.review_id, _corrected())

    assert len(parked.jobs()) == 1
    parked.run_job()
    with pytest.raises(ConflictError, match="resolved, not open"):
        parked.answer(parked.review_id, _corrected())


def test_an_answer_the_worker_cannot_apply_goes_back_to_a_person(parked: Parked) -> None:
    parked.answer(parked.review_id, _corrected())
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
    parked.answer(parked.review_id, _corrected())
    parked.harness.resolve(parked.review_id, _corrected())

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
    parked.answer(parked.review_id, _corrected())
    stop.armed = True

    assert parked.run_job() is True

    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.RESOLVED)
    with pytest.raises(ConflictError, match="not open"):
        parked.answer(parked.review_id, replace(_corrected(), store_name="Other"))
    assert len(parked.jobs()) == 1
    parked.harness.resume(parked.receipt_id)
    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert parked.harness.receipt(parked.receipt_id).store_name == "Costco"


def test_giving_back_a_failed_answer_never_demotes_a_receipt_completed_meanwhile(
    parked: Parked,
) -> None:
    parked.answer(parked.review_id, _corrected())
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
        parked.answer(parked.review_id, _corrected())

    assert parked.pending() == []
    assert (parked.state(), parked.jobs()) == before
    RetryReceipt(parked.harness.uow_factory)(parked.receipt_id)
    assert parked.run_job() is True
    assert parked.pending() == [parked.review_id]
    parked.answer(parked.review_id, _corrected())
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
        parked.answer(parked.review_id, replace(_corrected(), store_name="Other"))

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert parked.jobs() == []
