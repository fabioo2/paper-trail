"""The worker's jobs: receipts uploaded through the API (``UploadReceipt``) run through the graph
the way ``paper-trail process`` runs a photo (ADR-0005), and a person's answers to reviews given
in the web app (``AnswerReview``) resume it the way ``paper-trail review resolve`` does."""

from datetime import timedelta

import structlog

from paper_trail.application.payloads import ReviewAnswerPayload
from paper_trail.application.process_receipt import (
    ProcessingStoppedError,
    ProcessReceiptFile,
    failure_reason,
)
from paper_trail.application.receipts import ResolveReview
from paper_trail.domain.enums import FailureReason, JobKind, ProcessingStage, ReceiptStatus
from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import Job, require_id
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.ports.trace import Tracer
from paper_trail.ports.unit_of_work import UnitOfWorkFactory

log = structlog.get_logger(__name__)

STOPPED_UNEXPECTEDLY = "the worker stopped unexpectedly while running this job"


class ProcessReceiptJobs:
    """A worker step: claim one job, process its receipt, settle the job.

    A job runs once. When a Claude read stops the run (no key, the API failed, the budget is
    reached) the receipt is FAILED and keeps its checkpoint, as on the CLI, and the job is
    FAILED with the reason, never retried by the queue: ``process --resume`` continues the
    receipt. Any other failure fails both the same way. A job whose receipt is no longer
    PROCESSING (it was processed by other means) succeeds without a run. While a job runs,
    ``renew_lease`` keeps it claimed; a job whose worker died is claimed again once its lease
    runs out and continues the receipt's thread, which uses up an attempt. A job whose dead
    workers used up its last attempt is FAILED instead, before the next claim
    (``_sweep_and_claim``), so a job that crashes its worker cannot run forever: its receipt
    FAILED as stopped unexpectedly, for a person to retry. The receipt's trace events are
    dropped after its job.

    A ``resolve_review`` job resolves its review with the answer it carries
    (``ResolveReview.apply``: given once, even when a worker died after giving it). When that
    fails, the job is FAILED with the reason and ``ResolveReview.give_back`` settles the
    review by the graph's checkpoint: answered again by a person while the graph has not taken
    the answer, else closed with the receipt FAILED (``process --resume`` continues it).
    """

    def __init__(  # noqa: PLR0913 - the use cases it runs and the job settings
        self,
        uow_factory: UnitOfWorkFactory,
        process: ProcessReceiptFile,
        tracer: Tracer,
        *,
        worker_id: str,
        lease: timedelta,
        resolve_review: ResolveReview,
    ) -> None:
        self._uow_factory = uow_factory
        self._process = process
        self._resolve_review = resolve_review
        self._tracer = tracer
        self._worker_id = worker_id
        self._lease = lease
        self._running: int | None = None

    def __call__(self) -> bool:
        """True when a job was run, False when there was none."""
        job = self._sweep_and_claim()
        if job is None:
            return False
        job_id = require_id(job.id)
        self._running = job_id
        with structlog.contextvars.bound_contextvars(job_id=job_id, receipt_id=job.receipt_id):
            try:
                error = self._run(job)
                self._settle(job_id, error)
            finally:
                self._running = None
                self._tracer.forget(job.receipt_id)
        return True

    def _sweep_and_claim(self) -> Job | None:
        """Recover the jobs whose dead workers used up their attempts (``_recover``), then
        claim one."""
        with self._uow_factory(read_only=True) as uow:
            abandoned = uow.jobs.abandoned()
        for lost in abandoned:
            try:
                self._recover(lost)
            except Exception as failure:  # one job's failure must not stop the worker
                log.error("job.recovery_failed", job_id=lost.id, error_type=type(failure).__name__)
        with self._uow_factory() as uow:
            job = uow.jobs.claim(self._worker_id, self._lease)
            uow.commit()
        return job

    def _recover(self, lost: Job) -> None:
        """One transaction per job: ``fail_abandoned`` is the compare-and-set that gives this
        worker the right to recover it (another sweeper already did: nothing happens), and
        the job's receipt, while still PROCESSING, is FAILED as stopped unexpectedly in the
        same transaction, whatever the job's kind. When the receipt's graph already took an
        answer to its OPEN review (a worker answering it, or the CLI's hold of ``review
        resolve``, died after giving it), the review is closed and the receipt failed instead,
        whatever its status (``ResolveReview.close_taken``), so no second answer is taken and
        then ignored; an answer not taken leaves the review as it is, for a retry. Any failure
        rolls both back, leaving the job to the next sweep."""
        taken = self._resolve_review.answer_taken(lost.receipt_id)
        with self._uow_factory() as uow:
            try:
                uow.jobs.fail_abandoned(require_id(lost.id), STOPPED_UNEXPECTEDLY)
            except ConflictError:
                return  # another worker recovered it
            receipt = uow.receipts.get(lost.receipt_id)
            review = uow.reviews.find_open(lost.receipt_id) if taken else None
            if review is not None:
                answer = (
                    None
                    if lost.payload is None
                    else ReviewAnswerPayload.model_validate(lost.payload)
                )
                self._resolve_review.close_taken(
                    uow,
                    require_id(review.id),
                    forced_reason=answer.reason if answer is not None and answer.force else None,
                    failure=FailureReason.STOPPED_UNEXPECTEDLY,
                )
            elif receipt is not None and receipt.status is ReceiptStatus.PROCESSING:
                uow.receipts.update_status(
                    lost.receipt_id,
                    ReceiptStatus.FAILED,
                    ProcessingStage.FAILED,
                    failure_reason=FailureReason.STOPPED_UNEXPECTEDLY,
                )
            uow.commit()
        log.error("job.abandoned", job_id=lost.id, receipt_id=lost.receipt_id)

    def renew_lease(self) -> None:
        """Extend the lease of the job that is running, if any (the heartbeat calls it)."""
        job_id = self._running
        if job_id is None:
            return
        try:
            with self._uow_factory() as uow:
                uow.jobs.extend_lease(job_id, self._worker_id, self._lease)
                uow.commit()
        except ConflictError:
            log.warning("job.lease_lost", job_id=job_id)

    def _run(self, job: Job) -> str | None:
        """Process the job's receipt; why it failed, or None."""
        if job.kind is JobKind.RESOLVE_REVIEW:
            return self._resolve(job)
        with self._uow_factory(read_only=True) as uow:
            receipt = uow.receipts.get(job.receipt_id)
        if receipt is None or receipt.status is not ReceiptStatus.PROCESSING:
            log.info("job.skipped", status=None if receipt is None else receipt.status)
            return None
        try:
            outcome = self._process.process_stored(receipt)
        except ProcessingStoppedError as stopped:
            return str(stopped)
        except Exception as error:  # one job's failure must not stop the worker
            log.error("job.crashed", error_type=type(error).__name__)
            with self._uow_factory() as uow:
                uow.receipts.update_status(
                    job.receipt_id,
                    ReceiptStatus.FAILED,
                    ProcessingStage.FAILED,
                    failure_reason=failure_reason(error),
                )
                uow.commit()
            return type(error).__name__
        return outcome.failure

    def _resolve(self, job: Job) -> str | None:
        """Resolve the job's review with its answer; why it failed, or None."""
        answer = ReviewAnswerPayload.model_validate(job.payload)
        try:
            self._resolve_review.apply(
                answer.review_id,
                None if answer.draft is None else answer.draft.to_draft(),
                force=answer.force,
                reason=answer.reason,
            )
        except Exception as error:  # one job's failure must not stop the worker
            log.error("job.crashed", error_type=type(error).__name__)
            self._give_back(job, failure_reason(error))
            return mask_pans(str(error)).text or type(error).__name__
        return None

    def _give_back(self, job: Job, failure: FailureReason) -> None:
        """Settle the review of a failed ``resolve_review`` job (``ResolveReview.give_back``)."""
        answer = ReviewAnswerPayload.model_validate(job.payload)
        self._resolve_review.give_back(
            answer.review_id,
            forced_reason=answer.reason if answer.force else None,
            failure=failure,
        )

    def _settle(self, job_id: int, error: str | None) -> None:
        """Complete or fail the job; another worker holding it now is logged, not raised."""
        try:
            with self._uow_factory() as uow:
                if error is None:
                    uow.jobs.complete(job_id, self._worker_id)
                else:
                    uow.jobs.fail(job_id, self._worker_id, error, retry_at=None)
                uow.commit()
        except ConflictError:
            log.warning("job.lease_lost", job_id=job_id)
            return
        log.info("job.finished", failed=error is not None)
