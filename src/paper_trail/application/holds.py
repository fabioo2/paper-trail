"""A receipt held by the CLI while it runs the receipt's graph itself (``process --resume``,
``process --reprocess``, ``review resolve``), the way the worker holds a job (ADR-0005).

The hold is a job: RUNNING under the CLI's name, taken in the transaction that starts the run
and only while the receipt has no job waiting or running (``JobQueue.hold``). So the worker
never runs the receipt meanwhile, and the web app's Try again and review answers are refused
as for any running job. Its lease is renewed in the background like a worker's; it is completed
when the run ends, or failed when it raises. A CLI killed mid-run leaves the job to expire:
the worker then fails it and its receipt as stopped unexpectedly, for a person to retry.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import structlog

from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import Job, require_id
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger(__name__)

# Renewed while the job runs; a dead worker's (or CLI's) job is taken back after it.
JOB_LEASE = timedelta(minutes=5)


class ReceiptHold:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        holder: str,
        renew_every_s: float,
        lease: timedelta = JOB_LEASE,
    ) -> None:
        self._uow_factory = uow_factory
        self._holder = holder
        self._renew_every_s = renew_every_s
        self._lease = lease

    @contextmanager
    def __call__(self, receipt_id: int) -> Iterator[None]:
        """Hold the receipt for the block (ConflictError, before it runs, while a job of it
        waits or runs)."""
        with self._uow_factory() as uow:
            job = self.take(uow, receipt_id)
            uow.commit()
        with self.keep(job):
            yield

    def take(self, uow: UnitOfWork, receipt_id: int) -> Job:
        """The hold's job, in ``uow``: the caller commits it with the writes that start its
        run. ConflictError while a job of the receipt waits or runs."""
        return uow.jobs.hold(receipt_id, self._holder, self._lease)

    @contextmanager
    def keep(self, job: Job) -> Iterator[None]:
        """Renew the committed ``job``'s lease during the block, then complete it, or fail it
        with the error's type when the block raises."""
        job_id = require_id(job.id)
        done = threading.Event()

        def renew() -> None:
            while not done.wait(self._renew_every_s):
                try:
                    with self._uow_factory() as uow:
                        uow.jobs.extend_lease(job_id, self._holder, self._lease)
                        uow.commit()
                except Exception:
                    log.exception("hold.renew_failed", job_id=job_id)

        thread = threading.Thread(target=renew, name="receipt-hold", daemon=True)
        thread.start()
        error: str | None = None
        try:
            yield
        except BaseException as raised:
            error = type(raised).__name__
            raise
        finally:
            done.set()
            thread.join()
            self._release(job_id, error)

    def _release(self, job_id: int, error: str | None) -> None:
        try:
            with self._uow_factory() as uow:
                if error is None:
                    uow.jobs.complete(job_id, self._holder)
                else:
                    uow.jobs.fail(job_id, self._holder, error, retry_at=None)
                uow.commit()
        except ConflictError:
            log.warning("hold.lost", job_id=job_id)
