"""In-memory job and review queues (implement ``paper_trail.ports.queues``) over a ``FakeState``."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta

from paper_trail.domain.enums import JobKind, JobStatus, ReviewReason, ReviewStatus
from paper_trail.domain.errors import ConflictError, NotFoundError
from paper_trail.domain.models import Job, JsonValue, ReviewItem
from paper_trail.domain.paging import Page, PageRequest
from paper_trail.ports.clock import Clock
from tests.fakes.state import FakeState, newest_first_page
from tests.support import require_id


class FakeJobQueue:
    def __init__(self, state: FakeState, clock: Clock) -> None:
        self._state = state
        self._clock = clock

    def enqueue(self, kind: JobKind, receipt_id: int, *, max_attempts: int) -> Job:
        now = self._clock.now()
        job = Job(
            id=self._state.next_id("jobs"),
            kind=kind,
            receipt_id=receipt_id,
            status=JobStatus.PENDING,
            attempts=0,
            max_attempts=max_attempts,
            available_at=now,
            locked_by=None,
            lease_expires_at=None,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        return self._save(job)

    def claim(self, worker_id: str, lease: timedelta) -> Job | None:
        now = self._clock.now()
        candidates = [job for job in self._state.jobs.values() if _claimable(job, now)]
        if not candidates:
            return None
        oldest = min(candidates, key=lambda job: (job.available_at, require_id(job.id)))
        return self._save(
            replace(
                oldest,
                status=JobStatus.RUNNING,
                locked_by=worker_id,
                lease_expires_at=now + lease,
                updated_at=now,
            )
        )

    def extend_lease(self, job_id: int, worker_id: str, lease: timedelta) -> None:
        job = self._owned(job_id, worker_id)
        now = self._clock.now()
        self._save(replace(job, lease_expires_at=now + lease, updated_at=now))

    def complete(self, job_id: int, worker_id: str) -> None:
        job = self._owned(job_id, worker_id)
        self._save(self._unlocked(job, status=JobStatus.SUCCEEDED))

    def fail(self, job_id: int, worker_id: str, error: str, *, retry_at: datetime | None) -> Job:
        job = self._owned(job_id, worker_id)
        attempts = job.attempts + 1
        if retry_at is not None and attempts < job.max_attempts:
            failed = self._unlocked(job, status=JobStatus.PENDING, available_at=retry_at)
        else:
            failed = self._unlocked(job, status=JobStatus.FAILED)
        return self._save(replace(failed, attempts=attempts, last_error=error))

    def release(self, job_id: int, worker_id: str, *, available_at: datetime) -> None:
        job = self._owned(job_id, worker_id)
        self._save(self._unlocked(job, status=JobStatus.PENDING, available_at=available_at))

    def pending_count(self) -> int:
        return sum(job.status is JobStatus.PENDING for job in self._state.jobs.values())

    def get(self, job_id: int) -> Job | None:
        return self._state.jobs.get(job_id)

    def _owned(self, job_id: int, worker_id: str) -> Job:
        job = self._state.jobs.get(job_id)
        if job is None:
            msg = f"job {job_id} does not exist"
            raise NotFoundError(msg)
        if job.status is not JobStatus.RUNNING or job.locked_by != worker_id:
            msg = f"job {job_id} is not held by {worker_id}"
            raise ConflictError(msg)
        return job

    def _unlocked(
        self, job: Job, *, status: JobStatus, available_at: datetime | None = None
    ) -> Job:
        return replace(
            job,
            status=status,
            available_at=job.available_at if available_at is None else available_at,
            locked_by=None,
            lease_expires_at=None,
            updated_at=self._clock.now(),
        )

    def _save(self, job: Job) -> Job:
        self._state.jobs[require_id(job.id)] = job
        return job


def _claimable(job: Job, now: datetime) -> bool:
    if job.status is JobStatus.PENDING:
        return job.available_at <= now
    return (
        job.status is JobStatus.RUNNING
        and job.lease_expires_at is not None
        and job.lease_expires_at < now
    )


class FakeReviewQueue:
    def __init__(self, state: FakeState, clock: Clock) -> None:
        self._state = state
        self._clock = clock

    def open(
        self,
        receipt_id: int,
        reason: ReviewReason,
        details: Mapping[str, JsonValue],
        thread_id: str | None,
    ) -> ReviewItem:
        if self.find_open(receipt_id) is not None:
            msg = f"receipt {receipt_id} already has an open review item"
            raise ConflictError(msg)
        item = ReviewItem(
            id=self._state.next_id("review_items"),
            receipt_id=receipt_id,
            reason=reason,
            details=dict(details),
            status=ReviewStatus.OPEN,
            thread_id=thread_id,
            created_at=self._clock.now(),
            resolved_at=None,
        )
        self._state.reviews[require_id(item.id)] = item
        return item

    def get(self, review_id: int) -> ReviewItem | None:
        return self._state.reviews.get(review_id)

    def find_open(self, receipt_id: int) -> ReviewItem | None:
        return next(
            (
                item
                for item in self._state.reviews.values()
                if item.receipt_id == receipt_id and item.status is ReviewStatus.OPEN
            ),
            None,
        )

    def list_open(self, page: PageRequest) -> Page[ReviewItem]:
        rows = [item for item in self._state.reviews.values() if item.status is ReviewStatus.OPEN]
        return newest_first_page(rows, page, lambda item: item)

    def resolve(self, review_id: int) -> ReviewItem:
        return self._close(review_id, ReviewStatus.RESOLVED)

    def dismiss(self, review_id: int) -> ReviewItem:
        return self._close(review_id, ReviewStatus.DISMISSED)

    def open_count(self) -> int:
        return sum(item.status is ReviewStatus.OPEN for item in self._state.reviews.values())

    def _close(self, review_id: int, status: ReviewStatus) -> ReviewItem:
        item = self._state.reviews.get(review_id)
        if item is None:
            msg = f"review item {review_id} does not exist"
            raise NotFoundError(msg)
        if item.status is not ReviewStatus.OPEN:
            msg = f"review item {review_id} is already {item.status}"
            raise ConflictError(msg)
        closed = replace(item, status=status, resolved_at=self._clock.now())
        self._state.reviews[review_id] = closed
        return closed
