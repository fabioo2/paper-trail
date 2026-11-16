"""The worker's ``process_receipt`` jobs: receipts uploaded through the API (``UploadReceipt``)
run through the graph the way ``paper-trail process`` runs a photo (ADR-0005)."""

from datetime import timedelta

import structlog

from paper_trail.application.process_receipt import ProcessingStoppedError, ProcessReceiptFile
from paper_trail.domain.enums import ProcessingStage, ReceiptStatus
from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import Job, require_id
from paper_trail.ports.trace import Tracer
from paper_trail.ports.unit_of_work import UnitOfWorkFactory

log = structlog.get_logger(__name__)


class ProcessReceiptJobs:
    """A worker step: claim one job, process its receipt, settle the job.

    A job runs once. When a Claude read stops the run (no key, the API failed, the budget is
    reached) the receipt is FAILED and keeps its checkpoint, as on the CLI, and the job is
    FAILED with the reason, never retried by the queue: ``process --resume`` continues the
    receipt. Any other failure fails both the same way. A job whose receipt is no longer
    PROCESSING (it was processed by other means) succeeds without a run. While a job runs,
    ``renew_lease`` keeps it claimed; a job whose worker died is claimed again once its lease
    runs out and continues the receipt's thread. The receipt's trace events are dropped after
    its job.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        process: ProcessReceiptFile,
        tracer: Tracer,
        *,
        worker_id: str,
        lease: timedelta,
    ) -> None:
        self._uow_factory = uow_factory
        self._process = process
        self._tracer = tracer
        self._worker_id = worker_id
        self._lease = lease
        self._running: int | None = None

    def __call__(self) -> bool:
        """True when a job was run, False when there was none."""
        with self._uow_factory() as uow:
            job = uow.jobs.claim(self._worker_id, self._lease)
            uow.commit()
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
                    job.receipt_id, ReceiptStatus.FAILED, ProcessingStage.FAILED
                )
                uow.commit()
            return type(error).__name__
        return outcome.failure

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
