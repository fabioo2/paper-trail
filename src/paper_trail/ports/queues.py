"""The durable job queue of the worker and the human review queue (PLAN 3.4, ADR-0005)."""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Protocol

from paper_trail.domain.enums import JobKind, ReviewReason
from paper_trail.domain.models import Job, JsonValue, ReviewItem
from paper_trail.domain.paging import Page, PageRequest


class JobQueue(Protocol):
    """Jobs are leased, not locked: a worker that dies loses its job when the lease expires.

    The owner of a job is the worker holding it RUNNING. Owner-only operations raise
    ConflictError for anyone else and NotFoundError for an unknown id.
    """

    def enqueue(self, kind: JobKind, receipt_id: int, *, max_attempts: int) -> Job:
        """A new PENDING job, available now."""
        ...

    def claim(self, worker_id: str, lease: timedelta) -> Job | None:
        """Atomically take the job with the earliest available_at (then lowest id) among
        PENDING jobs with available_at <= now and RUNNING jobs whose lease expired before now.
        It becomes RUNNING, locked by ``worker_id`` until now + ``lease``. None if idle."""
        ...

    def extend_lease(self, job_id: int, worker_id: str, lease: timedelta) -> None:
        """Owner only: the lease now ends at now + ``lease``."""
        ...

    def complete(self, job_id: int, worker_id: str) -> None:
        """Owner only: SUCCEEDED."""
        ...

    def fail(self, job_id: int, worker_id: str, error: str, *, retry_at: datetime | None) -> Job:
        """Owner only: one more attempt is used up. PENDING again at ``retry_at`` while attempts
        remain, else FAILED; ``last_error`` records ``error``."""
        ...

    def release(self, job_id: int, worker_id: str, *, available_at: datetime) -> None:
        """Owner only: back to PENDING at ``available_at`` without using an attempt."""
        ...

    def pending_count(self) -> int:
        """PENDING jobs, those scheduled for later included."""
        ...

    def get(self, job_id: int) -> Job | None: ...


class ReviewQueue(Protocol):
    def open(
        self,
        receipt_id: int,
        reason: ReviewReason,
        details: Mapping[str, JsonValue],
        thread_id: str | None,
    ) -> ReviewItem:
        """A new OPEN item; ConflictError if the receipt already has one."""
        ...

    def get(self, review_id: int) -> ReviewItem | None: ...

    def find_open(self, receipt_id: int) -> ReviewItem | None:
        """The receipt's OPEN item (there is at most one), or None."""
        ...

    def list_open(self, page: PageRequest) -> Page[ReviewItem]:
        """OPEN items, newest first."""
        ...

    def resolve(self, review_id: int) -> ReviewItem:
        """OPEN -> RESOLVED, stamping resolved_at. ConflictError if not OPEN."""
        ...

    def dismiss(self, review_id: int) -> ReviewItem:
        """OPEN -> DISMISSED, stamping resolved_at. ConflictError if not OPEN."""
        ...

    def open_count(self) -> int: ...
