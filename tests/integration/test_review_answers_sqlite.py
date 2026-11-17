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

from paper_trail.application.jobs import ProcessReceiptJobs
from paper_trail.application.receipts import (
    AnswerReview,
    InconsistentDraftError,
    ListPendingReviews,
)
from paper_trail.domain.enums import (
    AgentRole,
    JobKind,
    JobStatus,
    ProcessingStage,
    ReceiptStatus,
    ReviewStatus,
)
from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import Job, JsonValue, ReceiptDraft
from paper_trail.ports.tools import ToolDescription, ToolGateway
from tests.agent_harness import Harness, agent_settings, build_harness
from tests.extractions import inconsistent_answer
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


def test_a_worker_that_died_after_the_run_finished_only_closes_the_review(parked: Parked) -> None:
    parked.answer(parked.review_id, _corrected())
    parked.harness.runner.resume(parked.receipt_id, _corrected(), attribute_discounts=False)
    assert parked.harness.receipt(parked.receipt_id).status is ReceiptStatus.COMPLETED

    assert parked.run_job() is True

    assert parked.state() == (ReceiptStatus.COMPLETED, ProcessingStage.DONE, ReviewStatus.RESOLVED)
    assert parked.jobs()[0].status is JobStatus.SUCCEEDED


def test_a_run_that_fails_after_the_answer_was_saved_is_continued_like_any_failed_run() -> None:
    stop = StopOnce(RuntimeError("matching failed"))
    parked = _park(stop)
    parked.answer(parked.review_id, _corrected())
    stop.armed = True

    assert parked.run_job() is True

    assert parked.jobs()[0].last_error == "matching failed"
    assert parked.state() == (ReceiptStatus.FAILED, ProcessingStage.FAILED, ReviewStatus.RESOLVED)
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


def test_an_open_review_of_a_failed_receipt_is_listed_and_answerable(parked: Parked) -> None:
    with parked.harness.uow_factory() as uow:
        uow.receipts.update_status(parked.receipt_id, ReceiptStatus.FAILED, ProcessingStage.FAILED)
        uow.commit()
    assert parked.pending() == [parked.review_id]

    parked.answer(parked.review_id, _corrected())
    assert parked.pending() == []
    parked.run_job()

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
