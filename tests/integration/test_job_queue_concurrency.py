"""Two workers claiming from the SQLite job queue at the same time (S1-AC-14).

Each worker thread uses its own pooled connection and unit of work, as two processes would. A
claim must be atomic (BEGIN IMMEDIATE + busy_timeout): no job is handed out twice and no worker
fails with "database is locked".
"""

import threading
from datetime import timedelta

from sqlalchemy import Engine

from paper_trail.application.process_receipt import RetryReceipt
from paper_trail.domain.enums import JobKind, ProcessingStage, ReceiptStatus
from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import Job
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.factories import receipt
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, sqlite_uow_factory
from tests.support import require_id

JOBS = 60
WORKERS = 4
LEASE = timedelta(minutes=5)


def _enqueue_all(uow_factory: UnitOfWorkFactory) -> set[int]:
    def enqueue(uow: UnitOfWork) -> set[int]:
        receipt_id = require_id(uow.receipts.add(receipt()).id)
        return {
            require_id(uow.jobs.enqueue(JobKind.PROCESS_RECEIPT, receipt_id, max_attempts=3).id)
            for _ in range(JOBS)
        }

    return in_unit(uow_factory, enqueue)


def test_s1_ac_14_concurrent_claims_never_return_the_same_job(migrated_engine: Engine) -> None:
    """S1-AC-14: four workers drain the queue; every job is claimed exactly once."""
    uow_factory = sqlite_uow_factory(migrated_engine, FakeClock())
    expected = _enqueue_all(uow_factory)
    claimed: dict[str, list[Job]] = {f"worker-{n}": [] for n in range(WORKERS)}
    errors: list[BaseException] = []
    start = threading.Barrier(WORKERS)

    def work(worker_id: str) -> None:
        try:
            start.wait()
            while (
                job := in_unit(uow_factory, lambda uow: uow.jobs.claim(worker_id, LEASE))
            ) is not None:
                claimed[worker_id].append(job)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=work, args=(worker,)) for worker in claimed]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    all_claims = [require_id(job.id) for jobs in claimed.values() for job in jobs]
    assert len(all_claims) == len(set(all_claims)), "a job was handed to two workers"
    assert set(all_claims) == expected
    for worker_id, jobs in claimed.items():
        assert all(job.locked_by == worker_id for job in jobs)
    with uow_factory() as uow:
        assert uow.jobs.pending_count() == 0


def test_h2_07_two_retries_at_once_queue_one_job(migrated_engine: Engine) -> None:
    """Hardening H2-07: "Try again" tapped twice; one retry wins, the other is a conflict."""
    uow_factory = sqlite_uow_factory(migrated_engine, FakeClock())
    failed = receipt(
        status=ReceiptStatus.FAILED, stage=ProcessingStage.FAILED, image_path="ab/cd/abcd.jpg"
    )
    receipt_id = require_id(in_unit(uow_factory, lambda uow: uow.receipts.add(failed)).id)
    retry = RetryReceipt(uow_factory)
    outcomes: list[str] = []
    start = threading.Barrier(2)

    def tap() -> None:
        start.wait()
        try:
            retry(receipt_id)
            outcomes.append("queued")
        except ConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=tap) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(outcomes) == ["conflict", "queued"]
    with uow_factory() as uow:
        assert uow.jobs.pending_count() == 1


def test_h4_06_two_workers_taking_back_one_expired_job_count_one_attempt(
    migrated_engine: Engine,
) -> None:
    clock = FakeClock()
    uow_factory = sqlite_uow_factory(migrated_engine, clock)

    def enqueue(uow: UnitOfWork) -> Job:
        receipt_id = require_id(uow.receipts.add(receipt()).id)
        return uow.jobs.enqueue(JobKind.PROCESS_RECEIPT, receipt_id, max_attempts=3)

    job = in_unit(uow_factory, enqueue)
    assert in_unit(uow_factory, lambda uow: uow.jobs.claim("dead-worker", LEASE)) is not None
    clock.advance(LEASE + timedelta(seconds=1))
    claimed: list[Job | None] = []
    start = threading.Barrier(WORKERS)

    def work(worker_id: str) -> None:
        start.wait()
        claimed.append(in_unit(uow_factory, lambda uow: uow.jobs.claim(worker_id, LEASE)))

    threads = [threading.Thread(target=work, args=(f"worker-{n}",)) for n in range(WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    taken = [found for found in claimed if found is not None]
    assert len(taken) == 1
    with uow_factory(read_only=True) as uow:
        stored = uow.jobs.get(require_id(job.id))
    assert stored is not None
    assert (stored.attempts, stored.locked_by) == (1, taken[0].locked_by)
