"""Two workers claiming from the SQLite job queue at the same time (S1-AC-14).

Each worker thread uses its own pooled connection and unit of work, as two processes would. A
claim must be atomic (BEGIN IMMEDIATE + busy_timeout): no job is handed out twice and no worker
fails with "database is locked".
"""

import threading
from datetime import timedelta

from sqlalchemy import Engine

from paper_trail.domain.enums import JobKind
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
