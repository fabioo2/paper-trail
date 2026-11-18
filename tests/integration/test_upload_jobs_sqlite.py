"""Uploaded receipts run by the worker's ``process_receipt`` jobs (``ProcessReceiptJobs``) on a
real SQLite file, with the graph wired from fakes and Claude scripted (``tests/agent_harness``).

Pinned: one job per upload runs the receipt the way ``paper-trail process`` does (completed,
parked for review, failed); a Claude read that stops the run (the API down, the budget
reached) leaves the receipt FAILED with its checkpoint and the job FAILED, never claimed again;
a job whose receipt is no longer PROCESSING succeeds without a read; a job whose worker died is
claimed again once its lease expires and continues the thread, while a running job's renewed
lease keeps it from a second worker; any other error fails the receipt and the job; the trace
events of a receipt are dropped after its job. An upload whose transaction fails leaves no
receipt; the CLI racing an upload of the same photo reports a duplicate.

Hardening: a person retries a FAILED receipt (``RetryReceipt``, H2-04 to H2-10): one job, which
continues its thread or reads its photo again, as often as the person asks; only a FAILED
receipt with a photo and no job waiting is retried. A job whose workers keep dying is failed
once its attempts are used up, its receipt FAILED as stopped unexpectedly in the same
transaction (H4-03 to H4-05).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from paper_trail.adapters.sqlite.receipt_repository import SqliteReceiptRepository
from paper_trail.application.budget import BudgetedReader
from paper_trail.application.jobs import STOPPED_UNEXPECTEDLY, ProcessReceiptJobs
from paper_trail.application.process_receipt import (
    UPLOAD_JOB_ATTEMPTS,
    ReceiptSource,
    RetryReceipt,
    UploadReceipt,
)
from paper_trail.domain.enums import (
    FailureReason,
    JobKind,
    JobStatus,
    ProcessingStage,
    ReceiptStatus,
)
from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import Job, Receipt
from paper_trail.ports.reader import ReaderUnavailableError, ReadRequest, ReadResult
from tests.agent_harness import Harness, build_harness
from tests.extractions import NOW, answer, inconsistent_answer
from tests.factories import receipt
from tests.fakes.budget import FakeSpendLedger
from tests.fakes.clock import FakeClock
from tests.fakes.images import FakeImageStore, fake_png
from tests.fakes.reader import ScriptedReader
from tests.support import require_id

WORKER = "worker-1"
LEASE = timedelta(minutes=30)


@dataclass
class Queue:
    harness: Harness
    upload: UploadReceipt
    run_job: ProcessReceiptJobs

    def uploaded(self, label: str = "costco") -> int:
        return require_id(self.upload(fake_png(label)).receipt.id)

    def state(self, receipt_id: int) -> tuple[ReceiptStatus, ProcessingStage]:
        receipt = self.harness.receipt(receipt_id)
        return receipt.status, receipt.stage

    def job(self, job_id: int = 1) -> Job:
        with self.harness.uow_factory(read_only=True) as uow:
            found = uow.jobs.get(job_id)
        assert found is not None
        return found

    def settled(self, job_id: int = 1) -> tuple[JobStatus, int, str | None, str | None]:
        job = self.job(job_id)
        return job.status, job.attempts, job.last_error, job.locked_by

    def failed(self, receipt_id: int) -> tuple[ReceiptStatus, FailureReason | None]:
        receipt = self.harness.receipt(receipt_id)
        return receipt.status, receipt.failure_reason

    def job_count(self) -> int:
        count = 0
        with self.harness.uow_factory(read_only=True) as uow:
            while uow.jobs.get(count + 1) is not None:
                count += 1
        return count

    def die_running(self, worker: str = "dead-worker") -> None:
        """A worker takes the next job and dies with it; its lease then runs out."""
        with self.harness.uow_factory() as uow:
            assert uow.jobs.claim(worker, LEASE) is not None
            uow.commit()
        self.harness.clock.advance(LEASE + timedelta(seconds=1))


class SlowClaude:
    """A read during which ``during`` runs first: the time a real read takes."""

    def __init__(self, inner: ScriptedReader) -> None:
        self.inner = inner
        self.during: Callable[[], None] = lambda: None

    def read[M](self, request: ReadRequest, schema: type[M]) -> ReadResult[M]:
        self.during()
        return self.inner.read(request, schema)


def _queue(
    reader: ScriptedReader | BudgetedReader | SlowClaude, clock: FakeClock | None = None
) -> Queue:
    harness = build_harness(reader, clock=clock)
    return Queue(
        harness=harness,
        upload=UploadReceipt(harness.images, harness.uow_factory, harness.clock),
        run_job=ProcessReceiptJobs(
            harness.uow_factory,
            harness.process,
            harness.tracer,
            worker_id=WORKER,
            lease=LEASE,
            resolve_review=harness.resolve,
        ),
    )


def _claim_as_other_worker(queue: Queue) -> Job | None:
    with queue.harness.uow_factory() as uow:
        job = uow.jobs.claim("other-worker", LEASE)
        uow.commit()
    return job


@pytest.mark.parametrize(
    ("replies", "then", "expected"),
    [
        ((answer(),), None, (ReceiptStatus.COMPLETED, ProcessingStage.DONE)),
        ((), inconsistent_answer(), (ReceiptStatus.NEEDS_REVIEW, ProcessingStage.NEEDS_REVIEW)),
    ],
    ids=["completed", "needs-review"],
)
def test_one_job_runs_an_uploaded_receipt_through_the_graph(
    replies: tuple[dict[str, object], ...],
    then: dict[str, object] | None,
    expected: tuple[ReceiptStatus, ProcessingStage],
) -> None:
    queue = _queue(ScriptedReader(*replies, then=then))
    receipt_id = queue.uploaded()

    assert queue.state(receipt_id) == (ReceiptStatus.PROCESSING, ProcessingStage.READING)
    assert queue.run_job() is True
    assert queue.state(receipt_id) == expected
    assert queue.settled() == (JobStatus.SUCCEEDED, 0, None, None)
    assert queue.harness.events(receipt_id) == ()
    assert queue.run_job() is False


def test_claude_down_fails_the_receipt_with_its_checkpoint_and_the_job_without_retry() -> None:
    reader = ScriptedReader(ReaderUnavailableError("Claude is down"))
    queue = _queue(reader)
    receipt_id = queue.uploaded()

    assert queue.run_job() is True

    assert queue.state(receipt_id) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    assert queue.settled() == (JobStatus.FAILED, 1, "Claude is down", None)
    assert queue.run_job() is False
    reader.script(answer())
    resumed = queue.harness.resume(receipt_id).graph
    assert resumed is not None
    assert resumed.status is ReceiptStatus.COMPLETED
    assert reader.calls == 2


def test_a_reached_budget_fails_the_receipt_and_the_job_without_a_read() -> None:
    clock = FakeClock(NOW)
    claude = ScriptedReader()
    queue = _queue(BudgetedReader(claude, FakeSpendLedger(), clock, Decimal(0)), clock)
    receipt_id = queue.uploaded()

    assert queue.run_job() is True

    status, attempts, error, _ = queue.settled()
    assert queue.state(receipt_id) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    assert (status, attempts) == (JobStatus.FAILED, 1)
    assert "budget" in str(error).lower()
    assert claude.calls == 0
    assert queue.run_job() is False


def test_a_run_that_fails_otherwise_fails_the_receipt_and_the_job() -> None:
    queue = _queue(ScriptedReader())
    receipt_id = queue.uploaded()
    queue.harness.images.files.clear()

    assert queue.run_job() is True

    status, attempts, error, _ = queue.settled()
    assert queue.state(receipt_id) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    assert (status, attempts) == (JobStatus.FAILED, 1)
    assert "no stored image" in str(error)


def test_a_receipt_processed_by_other_means_settles_its_job_without_a_read() -> None:
    reader = ScriptedReader()
    queue = _queue(reader)
    receipt_id = queue.uploaded()
    with queue.harness.uow_factory() as uow:
        uow.receipts.update_status(receipt_id, ReceiptStatus.COMPLETED, ProcessingStage.DONE)
        uow.commit()

    assert queue.run_job() is True

    assert queue.settled() == (JobStatus.SUCCEEDED, 0, None, None)
    assert reader.calls == 0


def test_the_job_of_a_worker_that_died_is_claimed_again_and_continues_the_thread() -> None:
    reader = ScriptedReader(answer())
    queue = _queue(reader)
    receipt_id = queue.uploaded()
    with queue.harness.uow_factory() as uow:
        assert uow.jobs.claim("dead-worker", LEASE) is not None
        uow.commit()
    image_path = queue.harness.receipt(receipt_id).image_path
    assert image_path is not None
    queue.harness.runner.start(
        receipt_id, source_name="dead.jpg", image_path=image_path, interrupt_after="extract"
    )

    assert queue.run_job() is False
    queue.harness.clock.advance(LEASE + timedelta(seconds=1))
    assert queue.run_job() is True

    assert queue.state(receipt_id) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert queue.settled() == (JobStatus.SUCCEEDED, 1, None, None)  # the dead worker's attempt
    assert reader.calls == 1


def test_an_unexpected_error_fails_the_job_and_the_worker_goes_on() -> None:
    """A receipt without a photo cannot be read (ConflictError): it and its job fail, so a
    client polling it stops; the next job runs."""
    queue = _queue(ScriptedReader(answer()))
    with queue.harness.uow_factory() as uow:
        photoless = require_id(
            uow.receipts.add(
                receipt(status=ReceiptStatus.PROCESSING, stage=ProcessingStage.READING, items=())
            ).id
        )
        uow.jobs.enqueue(JobKind.PROCESS_RECEIPT, photoless, max_attempts=1)
        uow.commit()
    uploaded = queue.uploaded()

    assert queue.run_job() is True
    assert queue.run_job() is True

    assert queue.settled(1) == (JobStatus.FAILED, 1, "ConflictError", None)
    assert queue.state(photoless) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    assert queue.settled(2) == (JobStatus.SUCCEEDED, 0, None, None)
    assert queue.state(uploaded) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)


def test_a_renewed_lease_keeps_a_long_job_from_a_second_worker() -> None:
    claude = SlowClaude(ScriptedReader(answer()))
    queue = _queue(claude)
    receipt_id = queue.uploaded()
    clock = queue.harness.clock
    other_claims: list[Job | None] = []

    def long_read() -> None:
        for _ in range(3):
            clock.advance(LEASE - timedelta(seconds=1))
            queue.run_job.renew_lease()
        other_claims.append(_claim_as_other_worker(queue))

    claude.during = long_read
    queue.run_job.renew_lease()
    assert queue.run_job() is True

    assert other_claims == [None]
    assert queue.state(receipt_id) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert queue.settled() == (JobStatus.SUCCEEDED, 0, None, None)


def test_a_job_another_worker_took_over_is_left_to_it() -> None:
    claude = SlowClaude(ScriptedReader(answer()))
    queue = _queue(claude)
    queue.uploaded()
    clock = queue.harness.clock

    def stalled_read() -> None:
        clock.advance(LEASE + timedelta(seconds=1))
        assert _claim_as_other_worker(queue) is not None
        queue.run_job.renew_lease()

    claude.during = stalled_read
    assert queue.run_job() is True

    status, _, _, locked_by = queue.settled()
    assert (status, locked_by) == (JobStatus.RUNNING, "other-worker")


class BrokenClock:
    def now(self) -> datetime:
        msg = "the transaction fails"
        raise RuntimeError(msg)


def test_an_upload_whose_transaction_fails_leaves_no_receipt() -> None:
    harness = build_harness(ScriptedReader())
    upload = UploadReceipt(harness.images, harness.uow_factory, BrokenClock())
    stored_sha = FakeImageStore().save(fake_png("costco")).sha256

    with pytest.raises(RuntimeError, match="the transaction fails"):
        upload(fake_png("costco"))

    with harness.uow_factory(read_only=True) as uow:
        assert uow.receipts.find_by_image_sha256(stored_sha) is None


def test_the_cli_racing_an_upload_of_the_same_photo_reports_a_duplicate() -> None:
    reader = ScriptedReader()
    queue = _queue(reader)
    process = queue.harness.process
    real_find = process._find

    uploads: list[int] = []

    def find_then_upload(sha256: str) -> Receipt | None:
        found = real_find(sha256)
        if not uploads:
            uploads.append(queue.uploaded())
        return found

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(process, "_find", find_then_upload)
        outcome = process(ReceiptSource(name="costco.jpg", image=fake_png("costco")))

    assert (outcome.receipt_id, outcome.duplicate, outcome.graph) == (1, True, None)
    assert reader.calls == 0


# --- H2-04 to H2-10: a person retries a FAILED receipt ----------------------------------------


def _failed_upload(reader: ScriptedReader) -> tuple[Queue, int]:
    """An uploaded receipt whose read failed: Claude was down."""
    reader.script(ReaderUnavailableError("Claude is down"))
    queue = _queue(reader)
    receipt_id = queue.uploaded()
    assert queue.run_job() is True
    assert queue.failed(receipt_id) == (ReceiptStatus.FAILED, FailureReason.CLAUDE_UNAVAILABLE)
    return queue, receipt_id


def test_h2_04_a_retry_queues_one_job_and_sets_the_receipt_processing() -> None:
    queue, receipt_id = _failed_upload(ScriptedReader())

    retried = RetryReceipt(queue.harness.uow_factory)(receipt_id)

    assert (retried.status, retried.stage, retried.failure_reason) == (
        ReceiptStatus.PROCESSING,
        ProcessingStage.QUEUED,
        None,
    )
    assert retried == queue.harness.receipt(receipt_id)
    assert queue.job_count() == 2
    second = queue.job(2)
    assert (second.kind, second.receipt_id, second.status, second.max_attempts) == (
        JobKind.PROCESS_RECEIPT,
        receipt_id,
        JobStatus.PENDING,
        UPLOAD_JOB_ATTEMPTS,
    )


def _set_status(queue: Queue, status: ReceiptStatus) -> Callable[[int], None]:
    def change(receipt_id: int) -> None:
        with queue.harness.uow_factory() as uow:
            uow.receipts.update_status(receipt_id, status, ProcessingStage.DONE)
            uow.commit()

    return change


def _pending_job(queue: Queue) -> Callable[[int], None]:
    def enqueue(receipt_id: int) -> None:
        with queue.harness.uow_factory() as uow:
            uow.jobs.enqueue(JobKind.PROCESS_RECEIPT, receipt_id, max_attempts=1)
            uow.commit()

    return enqueue


def _running_job(queue: Queue) -> Callable[[int], None]:
    def claim(receipt_id: int) -> None:
        _pending_job(queue)(receipt_id)
        assert _claim_as_other_worker(queue) is not None

    return claim


@pytest.mark.parametrize(
    ("arrange", "message"),
    [
        (lambda queue: _set_status(queue, ReceiptStatus.PROCESSING), "is processing"),
        (lambda queue: _set_status(queue, ReceiptStatus.NEEDS_REVIEW), "is needs_review"),
        (lambda queue: _set_status(queue, ReceiptStatus.COMPLETED), "is completed"),
        (_pending_job, "already being processed"),
        (_running_job, "already being processed"),
    ],
    ids=["processing", "needs-review", "completed", "pending-job", "running-job"],
)
def test_h2_05_a_retry_is_refused_unless_failed_with_no_job_waiting(
    arrange: Callable[[Queue], Callable[[int], None]], message: str
) -> None:
    queue, receipt_id = _failed_upload(ScriptedReader())
    arrange(queue)(receipt_id)
    before, jobs = queue.harness.receipt(receipt_id), queue.job_count()

    with pytest.raises(ConflictError, match=message):
        RetryReceipt(queue.harness.uow_factory)(receipt_id)

    assert (queue.harness.receipt(receipt_id), queue.job_count()) == (before, jobs)


def test_h2_05_a_failed_receipt_without_a_photo_is_not_retried() -> None:
    queue = _queue(ScriptedReader())
    with queue.harness.uow_factory() as uow:
        photoless = require_id(
            uow.receipts.add(
                receipt(status=ReceiptStatus.FAILED, stage=ProcessingStage.FAILED, items=())
            ).id
        )
        uow.commit()

    with pytest.raises(ConflictError, match="no stored photo"):
        RetryReceipt(queue.harness.uow_factory)(photoless)

    assert queue.job_count() == 0
    assert queue.failed(photoless) == (ReceiptStatus.FAILED, None)


def test_h2_08_a_retry_whose_transaction_fails_changes_nothing() -> None:
    queue, receipt_id = _failed_upload(ScriptedReader())
    before = queue.harness.receipt(receipt_id)

    def broken(*_: object, **__: object) -> None:
        msg = "the transaction fails"
        raise RuntimeError(msg)

    with (
        pytest.MonkeyPatch.context() as patch,
        pytest.raises(RuntimeError, match="the transaction fails"),
    ):
        patch.setattr(SqliteReceiptRepository, "update_status", broken)
        RetryReceipt(queue.harness.uow_factory)(receipt_id)

    assert (queue.harness.receipt(receipt_id), queue.job_count()) == (before, 1)


def test_h2_09_a_retry_continues_the_thread_without_reading_again() -> None:
    reader = ScriptedReader(answer())
    queue = _queue(reader)
    receipt_id = queue.uploaded()
    image_path = queue.harness.receipt(receipt_id).image_path
    assert image_path is not None
    queue.harness.runner.start(
        receipt_id, source_name="costco.jpg", image_path=image_path, interrupt_after="extract"
    )
    queue.die_running()
    queue.die_running()
    assert queue.run_job() is False
    assert queue.failed(receipt_id) == (ReceiptStatus.FAILED, FailureReason.STOPPED_UNEXPECTEDLY)

    RetryReceipt(queue.harness.uow_factory)(receipt_id)
    assert queue.run_job() is True

    assert queue.failed(receipt_id) == (ReceiptStatus.COMPLETED, None)
    assert queue.settled(2) == (JobStatus.SUCCEEDED, 0, None, None)
    assert reader.calls == 1


def test_h2_09_a_retry_without_a_checkpoint_reads_the_stored_photo_again() -> None:
    reader = ScriptedReader(answer())
    queue = _queue(reader)
    receipt_id = queue.uploaded()
    queue.die_running()
    queue.die_running()
    assert queue.run_job() is False
    assert reader.calls == 0

    RetryReceipt(queue.harness.uow_factory)(receipt_id)
    assert queue.run_job() is True

    assert queue.failed(receipt_id) == (ReceiptStatus.COMPLETED, None)
    assert reader.calls == 1
    assert len(queue.harness.images.files) == 1
    with queue.harness.uow_factory(read_only=True) as uow:
        assert uow.receipts.get(receipt_id + 1) is None


def test_h2_10_a_retry_that_fails_again_keeps_its_reason_and_can_be_retried() -> None:
    clock = FakeClock(NOW)
    claude = ScriptedReader()
    queue = _queue(BudgetedReader(claude, FakeSpendLedger(), clock, Decimal(0)), clock)
    receipt_id = queue.uploaded()
    retry = RetryReceipt(queue.harness.uow_factory)

    for _ in range(2):
        assert queue.run_job() is True
        assert queue.failed(receipt_id) == (ReceiptStatus.FAILED, FailureReason.BUDGET_REACHED)
        retry(receipt_id)

    assert queue.harness.receipt(receipt_id).status is ReceiptStatus.PROCESSING
    assert queue.job_count() == 3
    assert claude.calls == 0


# --- H4-03 to H4-05: a job whose workers keep dying ------------------------------------------


def test_h4_03_an_exhausted_job_fails_its_receipt_as_stopped_unexpectedly() -> None:
    queue = _queue(ScriptedReader(answer()))
    receipt_id = queue.uploaded()
    for _ in range(UPLOAD_JOB_ATTEMPTS):
        queue.die_running()

    assert queue.run_job() is False

    assert queue.settled() == (JobStatus.FAILED, UPLOAD_JOB_ATTEMPTS, STOPPED_UNEXPECTEDLY, None)
    assert queue.state(receipt_id) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    assert queue.failed(receipt_id) == (ReceiptStatus.FAILED, FailureReason.STOPPED_UNEXPECTEDLY)
    RetryReceipt(queue.harness.uow_factory)(receipt_id)
    assert queue.run_job() is True
    assert queue.state(receipt_id) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)


def test_h4_03_the_job_and_its_receipt_fail_in_one_transaction() -> None:
    queue = _queue(ScriptedReader(answer()))
    receipt_id = queue.uploaded()
    for _ in range(UPLOAD_JOB_ATTEMPTS):
        queue.die_running()

    def broken(*_: object, **__: object) -> None:
        msg = "the transaction fails"
        raise RuntimeError(msg)

    with (
        pytest.MonkeyPatch.context() as patch,
        pytest.raises(RuntimeError, match="the transaction fails"),
    ):
        patch.setattr(SqliteReceiptRepository, "update_status", broken)
        queue.run_job()

    assert queue.settled()[0] is JobStatus.RUNNING
    assert queue.state(receipt_id) == (ReceiptStatus.PROCESSING, ProcessingStage.READING)


def test_h4_04_an_exhausted_job_does_not_hold_up_the_next_one() -> None:
    queue = _queue(ScriptedReader(answer()))
    lost = queue.uploaded("lost")
    for _ in range(UPLOAD_JOB_ATTEMPTS):
        queue.die_running()
    waiting = queue.uploaded("waiting")

    assert queue.run_job() is True

    assert queue.failed(lost) == (ReceiptStatus.FAILED, FailureReason.STOPPED_UNEXPECTEDLY)
    assert queue.state(waiting) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert queue.settled(2) == (JobStatus.SUCCEEDED, 0, None, None)


def test_h4_05_a_late_worker_cannot_settle_a_job_failed_as_exhausted() -> None:
    claude = SlowClaude(ScriptedReader(answer()))
    queue = _queue(claude)
    receipt_id = queue.uploaded()
    clock = queue.harness.clock
    other = ProcessReceiptJobs(
        queue.harness.uow_factory,
        queue.harness.process,
        queue.harness.tracer,
        worker_id="other-worker",
        lease=LEASE,
        resolve_review=queue.harness.resolve,
    )

    def stalled_read() -> None:
        clock.advance(LEASE + timedelta(seconds=1))
        assert _claim_as_other_worker(queue) is not None
        clock.advance(LEASE + timedelta(seconds=1))
        assert other() is False
        assert queue.failed(receipt_id) == (
            ReceiptStatus.FAILED,
            FailureReason.STOPPED_UNEXPECTEDLY,
        )

    claude.during = stalled_read
    assert queue.run_job() is True

    status, attempts, error, _ = queue.settled()
    assert (status, attempts, error) == (
        JobStatus.FAILED,
        UPLOAD_JOB_ATTEMPTS,
        STOPPED_UNEXPECTEDLY,
    )
    assert queue.failed(receipt_id) == (ReceiptStatus.COMPLETED, None)
