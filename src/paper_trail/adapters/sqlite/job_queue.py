"""``JobQueue`` on SQLite: the ``jobs`` table as a leased work queue (ADR-0005).

``claim`` is one UPDATE ... RETURNING whose subquery picks the job, so choosing and taking it
is a single atomic statement; together with the unit of work's BEGIN IMMEDIATE no two workers
can ever take the same job.
"""

from datetime import datetime, timedelta

from sqlalchemy import Connection, and_, func, insert, or_, select, update

from paper_trail.adapters.sqlite.mappers import job_from_row
from paper_trail.adapters.sqlite.schema import jobs
from paper_trail.domain.enums import JobKind, JobStatus
from paper_trail.domain.errors import ConflictError, NotFoundError
from paper_trail.domain.models import Job
from paper_trail.ports.clock import Clock


class SqliteJobQueue:
    """Implements ``paper_trail.ports.queues.JobQueue``."""

    def __init__(self, connection: Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock

    def enqueue(self, kind: JobKind, receipt_id: int, *, max_attempts: int) -> Job:
        now = self._clock.now()
        row = (
            self._connection.execute(
                insert(jobs)
                .values(
                    kind=kind,
                    receipt_id=receipt_id,
                    status=JobStatus.PENDING,
                    attempts=0,
                    max_attempts=max_attempts,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
                .returning(jobs)
            )
            .mappings()
            .one()
        )
        return job_from_row(row)

    def claim(self, worker_id: str, lease: timedelta) -> Job | None:
        now = self._clock.now()
        next_job = (
            select(jobs.c.id)
            .where(
                or_(
                    and_(jobs.c.status == JobStatus.PENDING, jobs.c.available_at <= now),
                    and_(jobs.c.status == JobStatus.RUNNING, jobs.c.lease_expires_at < now),
                )
            )
            .order_by(jobs.c.available_at, jobs.c.id)
            .limit(1)
            .scalar_subquery()
        )
        row = (
            self._connection.execute(
                update(jobs)
                .where(jobs.c.id == next_job)
                .values(
                    status=JobStatus.RUNNING,
                    locked_by=worker_id,
                    lease_expires_at=now + lease,
                    updated_at=now,
                )
                .returning(jobs)
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else job_from_row(row)

    def extend_lease(self, job_id: int, worker_id: str, lease: timedelta) -> None:
        self._owned(job_id, worker_id)
        now = self._clock.now()
        self._set(job_id, lease_expires_at=now + lease, updated_at=now)

    def complete(self, job_id: int, worker_id: str) -> None:
        self._owned(job_id, worker_id)
        self._finish(job_id, JobStatus.SUCCEEDED)

    def fail(self, job_id: int, worker_id: str, error: str, *, retry_at: datetime | None) -> Job:
        job = self._owned(job_id, worker_id)
        attempts = job.attempts + 1
        if retry_at is not None and attempts < job.max_attempts:
            return self._finish(
                job_id,
                JobStatus.PENDING,
                available_at=retry_at,
                attempts=attempts,
                last_error=error,
            )
        return self._finish(job_id, JobStatus.FAILED, attempts=attempts, last_error=error)

    def release(self, job_id: int, worker_id: str, *, available_at: datetime) -> None:
        self._owned(job_id, worker_id)
        self._finish(job_id, JobStatus.PENDING, available_at=available_at)

    def pending_count(self) -> int:
        count: int = self._connection.execute(
            select(func.count()).select_from(jobs).where(jobs.c.status == JobStatus.PENDING)
        ).scalar_one()
        return count

    def get(self, job_id: int) -> Job | None:
        row = (
            self._connection.execute(select(jobs).where(jobs.c.id == job_id))
            .mappings()
            .one_or_none()
        )
        return None if row is None else job_from_row(row)

    def _owned(self, job_id: int, worker_id: str) -> Job:
        """The job, if ``worker_id`` holds it RUNNING (an expired lease still counts)."""
        job = self.get(job_id)
        if job is None:
            msg = f"job {job_id} does not exist"
            raise NotFoundError(msg)
        if job.status is not JobStatus.RUNNING or job.locked_by != worker_id:
            msg = f"job {job_id} is not held by {worker_id}"
            raise ConflictError(msg)
        return job

    def _finish(self, job_id: int, status: JobStatus, **values: object) -> Job:
        """Leave RUNNING for ``status``, unlocking the job."""
        return self._set(
            job_id,
            status=status,
            locked_by=None,
            lease_expires_at=None,
            updated_at=self._clock.now(),
            **values,
        )

    def _set(self, job_id: int, **values: object) -> Job:
        row = (
            self._connection.execute(
                update(jobs).where(jobs.c.id == job_id).values(values).returning(jobs)
            )
            .mappings()
            .one()
        )
        return job_from_row(row)
