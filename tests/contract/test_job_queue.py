"""JobQueue contract (S1-AC-10, S1-AC-14), for the fake and the SQLite adapter.

The race between two claiming workers needs real connections and lives in
``tests/integration/test_job_queue_concurrency.py``.

Pinned (docs/tests/stage1.md): ``claim`` takes the candidate with the earliest available_at
(then lowest id), does not count an attempt, and leases until now + lease; ownership means
RUNNING and locked by the caller (an expired lease is still owned until another worker claims
the job); finishing a job clears locked_by and lease_expires_at; unknown ids raise NotFoundError.
"""

from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from paper_trail.domain.enums import JobKind, JobStatus
from paper_trail.domain.errors import ConflictError, NotFoundError
from paper_trail.domain.models import Job
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.factories import receipt
from tests.fakes.clock import FakeClock
from tests.storage import in_unit
from tests.support import require_id

LEASE = timedelta(minutes=5)
WORKER = "worker-a"
OTHER = "worker-b"


def _receipt_id(uow_factory: UnitOfWorkFactory) -> int:
    return require_id(in_unit(uow_factory, lambda uow: uow.receipts.add(receipt())).id)


def _enqueue(uow_factory: UnitOfWorkFactory, max_attempts: int = 3) -> Job:
    receipt_id = _receipt_id(uow_factory)
    return in_unit(
        uow_factory,
        lambda uow: uow.jobs.enqueue(
            JobKind.PROCESS_RECEIPT, receipt_id, max_attempts=max_attempts
        ),
    )


def _claim(uow_factory: UnitOfWorkFactory, worker: str = WORKER) -> Job | None:
    return in_unit(uow_factory, lambda uow: uow.jobs.claim(worker, LEASE))


def _get(uow_factory: UnitOfWorkFactory, job: Job) -> Job:
    with uow_factory() as uow:
        loaded = uow.jobs.get(require_id(job.id))
    assert loaded is not None
    return loaded


def _run(uow_factory: UnitOfWorkFactory, action: Callable[[UnitOfWork], object]) -> None:
    in_unit(uow_factory, action)


def _fail(uow_factory: UnitOfWorkFactory, job: Job, error: str, retry_at: datetime | None) -> Job:
    return in_unit(
        uow_factory, lambda uow: uow.jobs.fail(require_id(job.id), WORKER, error, retry_at=retry_at)
    )


def _release(uow_factory: UnitOfWorkFactory, job: Job, available_at: datetime) -> None:
    _run(
        uow_factory,
        lambda uow: uow.jobs.release(require_id(job.id), WORKER, available_at=available_at),
    )


def test_s1_ac_10_enqueue_creates_a_pending_job(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    """S1-AC-10: a new job is PENDING, available now, unlocked, with no attempts."""
    job = _enqueue(uow_factory, max_attempts=5)

    assert job.id is not None
    assert (job.kind, job.status, job.attempts, job.max_attempts) == (
        JobKind.PROCESS_RECEIPT,
        JobStatus.PENDING,
        0,
        5,
    )
    assert job.available_at == job.created_at == job.updated_at == fake_clock.now()
    assert (job.locked_by, job.lease_expires_at, job.last_error) == (None, None, None)
    assert _get(uow_factory, job) == job


def test_s1_ac_10_get_unknown_job_is_none(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: get() of an unknown id is None."""
    with uow_factory() as uow:
        assert uow.jobs.get(777) is None


def test_s1_ac_14_claim_on_an_empty_queue_is_none(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-14: nothing to do -> None."""
    assert _claim(uow_factory) is None


def test_s1_ac_14_claim_takes_the_oldest_pending_job_and_leases_it(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    """S1-AC-14: oldest available PENDING job becomes RUNNING, locked, with a lease."""
    first = _enqueue(uow_factory)
    fake_clock.advance(1)
    second = _enqueue(uow_factory)
    fake_clock.advance(1)

    claimed = _claim(uow_factory)

    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.status is JobStatus.RUNNING
    assert claimed.locked_by == WORKER
    assert claimed.lease_expires_at == fake_clock.now() + LEASE
    assert claimed.attempts == 0
    assert _get(uow_factory, first) == claimed
    next_claim = _claim(uow_factory, OTHER)
    assert next_claim is not None
    assert next_claim.id == second.id
    assert _claim(uow_factory) is None


def test_s1_ac_14_jobs_scheduled_in_the_future_are_not_claimable(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    """S1-AC-14: only jobs with available_at <= now are handed out."""
    job = _enqueue(uow_factory)
    assert _claim(uow_factory) is not None
    retry_at = fake_clock.now() + timedelta(minutes=10)
    _release(uow_factory, job, retry_at)

    assert _claim(uow_factory) is None
    fake_clock.set(retry_at)
    claimed = _claim(uow_factory)
    assert claimed is not None
    assert claimed.id == job.id


def test_s1_ac_14_expired_lease_makes_a_running_job_claimable(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    """S1-AC-14: a RUNNING job whose lease has expired is claimed again by another worker."""
    job = _enqueue(uow_factory)
    assert _claim(uow_factory) is not None

    fake_clock.advance(LEASE)
    assert _claim(uow_factory, OTHER) is None, "lease_expires_at < now is required, not <="

    fake_clock.advance(timedelta(seconds=1))
    reclaimed = _claim(uow_factory, OTHER)

    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.locked_by == OTHER
    assert reclaimed.lease_expires_at == fake_clock.now() + LEASE
    with pytest.raises(ConflictError):
        _run(uow_factory, lambda uow: uow.jobs.complete(require_id(job.id), WORKER))


def test_s1_ac_14_extend_lease_by_the_owner(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    """S1-AC-14: the owner can extend its lease, which keeps the job from other workers."""
    job = _enqueue(uow_factory)
    assert _claim(uow_factory) is not None
    fake_clock.advance(timedelta(minutes=4))
    extended_at = fake_clock.now()

    _run(uow_factory, lambda uow: uow.jobs.extend_lease(require_id(job.id), WORKER, LEASE))
    fake_clock.advance(timedelta(minutes=2))

    assert _get(uow_factory, job).lease_expires_at == extended_at + LEASE
    assert _claim(uow_factory, OTHER) is None


@pytest.mark.parametrize("operation", ["extend_lease", "complete", "fail", "release"])
def test_s1_ac_14_only_the_owner_may_touch_a_running_job(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock, operation: str
) -> None:
    """S1-AC-14: extend_lease/complete (and fail/release) by a non-owner raise ConflictError."""
    job = _enqueue(uow_factory)
    assert _claim(uow_factory) is not None
    job_id = require_id(job.id)
    actions: dict[str, Callable[[UnitOfWork], object]] = {
        "extend_lease": lambda uow: uow.jobs.extend_lease(job_id, OTHER, LEASE),
        "complete": lambda uow: uow.jobs.complete(job_id, OTHER),
        "fail": lambda uow: uow.jobs.fail(job_id, OTHER, "boom", retry_at=fake_clock.now()),
        "release": lambda uow: uow.jobs.release(job_id, OTHER, available_at=fake_clock.now()),
    }
    before = _get(uow_factory, job)

    with pytest.raises(ConflictError):
        _run(uow_factory, actions[operation])

    assert _get(uow_factory, job) == before


def test_s1_ac_14_a_job_that_is_not_running_has_no_owner(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-14: completing a PENDING (never claimed) job is a conflict."""
    job = _enqueue(uow_factory)

    with pytest.raises(ConflictError):
        _run(uow_factory, lambda uow: uow.jobs.complete(require_id(job.id), WORKER))


@pytest.mark.parametrize("operation", ["extend_lease", "complete", "fail", "release"])
def test_s1_ac_14_unknown_job_raises_not_found(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock, operation: str
) -> None:
    """Pinned: operations on a job id that does not exist raise NotFoundError."""
    actions: dict[str, Callable[[UnitOfWork], object]] = {
        "extend_lease": lambda uow: uow.jobs.extend_lease(404, WORKER, LEASE),
        "complete": lambda uow: uow.jobs.complete(404, WORKER),
        "fail": lambda uow: uow.jobs.fail(404, WORKER, "boom", retry_at=None),
        "release": lambda uow: uow.jobs.release(404, WORKER, available_at=fake_clock.now()),
    }

    with pytest.raises(NotFoundError):
        _run(uow_factory, actions[operation])


def test_s1_ac_14_complete_marks_succeeded_and_unlocks(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    """S1-AC-14: complete -> SUCCEEDED, lock and lease cleared; it is never claimed again."""
    job = _enqueue(uow_factory)
    assert _claim(uow_factory) is not None
    fake_clock.advance(2)

    _run(uow_factory, lambda uow: uow.jobs.complete(require_id(job.id), WORKER))

    done = _get(uow_factory, job)
    assert done.status is JobStatus.SUCCEEDED
    assert (done.locked_by, done.lease_expires_at) == (None, None)
    assert done.updated_at == fake_clock.now()
    fake_clock.advance(LEASE * 2)
    assert _claim(uow_factory) is None


def test_s1_ac_14_fail_with_retry_reschedules_until_max_attempts(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    """S1-AC-14: fail(retry_at) -> PENDING at retry_at until attempts reach max, then FAILED."""
    job = _enqueue(uow_factory, max_attempts=3)

    for attempt in (1, 2):
        assert _claim(uow_factory) is not None
        retry_at = fake_clock.now() + timedelta(seconds=30 * attempt)
        failed = _fail(uow_factory, job, f"error {attempt}", retry_at)
        assert failed == _get(uow_factory, job)
        assert failed.status is JobStatus.PENDING
        assert failed.attempts == attempt
        assert failed.available_at == retry_at
        assert failed.last_error == f"error {attempt}"
        assert (failed.locked_by, failed.lease_expires_at) == (None, None)
        fake_clock.set(retry_at)

    assert _claim(uow_factory) is not None
    final = _fail(uow_factory, job, "error 3", fake_clock.now())

    assert final.status is JobStatus.FAILED
    assert final.attempts == 3
    assert final.last_error == "error 3"
    assert (final.locked_by, final.lease_expires_at) == (None, None)
    fake_clock.advance(LEASE * 2)
    assert _claim(uow_factory) is None


def test_s1_ac_14_fail_without_retry_is_final(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-14: fail(retry_at=None) marks the job FAILED at once, even with attempts left."""
    job = _enqueue(uow_factory, max_attempts=5)
    assert _claim(uow_factory) is not None

    failed = _fail(uow_factory, job, "unreadable image", None)

    assert (failed.status, failed.attempts, failed.last_error) == (
        JobStatus.FAILED,
        1,
        "unreadable image",
    )


def test_s1_ac_14_release_does_not_consume_an_attempt(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    """S1-AC-14: release -> PENDING at available_at, unlocked, attempts unchanged."""
    job = _enqueue(uow_factory, max_attempts=1)
    later = fake_clock.now() + timedelta(minutes=1)

    for _ in range(3):
        assert _claim(uow_factory) is not None
        _release(uow_factory, job, later)
        fake_clock.set(later)
        later += timedelta(minutes=1)

    released = _get(uow_factory, job)
    assert released.status is JobStatus.PENDING
    assert released.attempts == 0
    assert (released.locked_by, released.lease_expires_at) == (None, None)
    assert released.available_at == fake_clock.now()


def test_s1_ac_10_pending_count(uow_factory: UnitOfWorkFactory, fake_clock: FakeClock) -> None:
    """Pinned: pending_count counts PENDING jobs, including those scheduled for later."""
    assert in_unit(uow_factory, lambda uow: uow.jobs.pending_count()) == 0
    first = _enqueue(uow_factory)
    _enqueue(uow_factory)
    _enqueue(uow_factory)
    assert _claim(uow_factory) is not None
    later = fake_clock.now() + timedelta(hours=1)
    _release(uow_factory, first, later)
    assert _claim(uow_factory) is not None

    assert in_unit(uow_factory, lambda uow: uow.jobs.pending_count()) == 2
