"""Processing one receipt photo: store it, create the receipt, run the graph (agent-core plan
2.2, claude-extraction plan V7, V11).

An image is stored once by content: the same photo again returns the receipt it already made.
The receipt exists, PROCESSING, before the graph starts, so a run that a Claude read stops half
way (the API failed, refused, was cut off, or the monthly budget is reached) leaves the receipt
FAILED with its checkpoint, to resume (``resume``). A stored photo can be read again on a new
thread (``reprocess``). A photo uploaded through the API is stored and its receipt created the
same way (``UploadReceipt``), and the worker runs the graph for it (``process_stored``); a person
queues a FAILED one for the worker again (``RetryReceipt``). A FAILED receipt keeps why, as a
``FailureReason`` (``failure_reason``), never the error's text.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import PurePosixPath

import structlog

from paper_trail.application.payloads import ReviewAnswerPayload
from paper_trail.application.receipts import remember_forced_reason
from paper_trail.domain.enums import (
    AgentRole,
    FailureReason,
    JobKind,
    ProcessingStage,
    ReceiptStatus,
    ReviewStatus,
)
from paper_trail.domain.errors import ConflictError, NotFoundError, UnsupportedMediaError
from paper_trail.domain.models import Receipt, ReviewItem, require_id
from paper_trail.domain.money import DEFAULT_CURRENCY
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.agents import (
    GraphOutcome,
    NothingToResumeError,
    OutdatedCheckpointError,
    ReceiptGraphRunner,
)
from paper_trail.ports.clock import Clock
from paper_trail.ports.images import ImageStore, StoredImage
from paper_trail.ports.reader import (
    BudgetReachedError,
    ReaderError,
    ReaderKeyError,
    ReaderUnavailableError,
)
from paper_trail.ports.trace import Tracer
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger(__name__)

# The worker runs an uploaded receipt once; a failed run is retried by a person (``RetryReceipt``,
# ``process --resume``). The second attempt is only for a worker that died mid-run: the queue
# takes its job back once, then fails it as stopped unexpectedly.
UPLOAD_JOB_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class ReceiptSource:
    """A receipt photo to process."""

    name: str  # shown in the trace, e.g. the file name
    image: bytes


class ProcessingStoppedError(ReaderError):
    """A Claude read stopped the run of ``receipt_id``: the receipt is FAILED and keeps its
    checkpoint, to be resumed."""

    def __init__(self, receipt_id: int, cause: ReaderError) -> None:
        super().__init__(mask_pans(str(cause)).text)
        self.receipt_id = receipt_id


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    receipt_id: int
    duplicate: bool  # the image was already processed: no new run
    graph: GraphOutcome | None  # None for a duplicate or a failed run
    failure: str | None = None  # why the run failed (the receipt is FAILED)


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    receipt: Receipt
    duplicate: bool  # the image was already stored: its receipt, no new job


class UploadReceipt:
    """A photo uploaded through the API: stored, its receipt created PROCESSING and one
    ``process_receipt`` job queued for the worker, in one transaction."""

    def __init__(
        self, image_store: ImageStore, uow_factory: UnitOfWorkFactory, clock: Clock
    ) -> None:
        self._image_store = image_store
        self._uow_factory = uow_factory
        self._clock = clock

    def __call__(self, image: bytes) -> UploadOutcome:
        """UnsupportedMediaError for bytes that are not an image (or one above the pixel limit),
        before any receipt exists. An image already stored is a duplicate: its receipt."""
        stored = self._image_store.save(image)
        with self._uow_factory() as uow:
            existing = uow.receipts.find_by_image_sha256(stored.sha256)
            if existing is not None:
                return UploadOutcome(receipt=existing, duplicate=True)
            receipt = uow.receipts.add(new_receipt(stored, self._clock.now()))
            receipt_id = require_id(receipt.id)
            uow.jobs.enqueue(JobKind.PROCESS_RECEIPT, receipt_id, max_attempts=UPLOAD_JOB_ATTEMPTS)
            uow.commit()
        log.info("receipt.uploaded", receipt_id=receipt_id)
        return UploadOutcome(receipt=receipt, duplicate=False)


class RetryReceipt:
    """A person tries a FAILED receipt again: it is PROCESSING again (QUEUED) and one
    ``process_receipt`` job is queued, in one transaction; the worker continues its thread
    from its checkpoint or reads its stored photo again (``process_stored``). NotFoundError
    for an unknown receipt; ConflictError, changing nothing, when it is not FAILED, a job for
    it is waiting or running, or it has no stored photo to read."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(self, receipt_id: int) -> Receipt:
        with self._uow_factory() as uow:
            receipt = uow.receipts.get(receipt_id)
            if receipt is None:
                msg = f"receipt {receipt_id} does not exist"
                raise NotFoundError(msg)
            if receipt.status is not ReceiptStatus.FAILED:
                msg = f"receipt {receipt_id} is {receipt.status.value}, not failed"
                raise ConflictError(msg)
            if any(uow.jobs.has_active(kind, receipt_id) for kind in JobKind):
                msg = f"receipt {receipt_id} is already being processed"
                raise ConflictError(msg)
            if receipt.image_path is None:
                msg = f"receipt {receipt_id} has no stored photo to read again"
                raise ConflictError(msg)
            uow.jobs.enqueue(JobKind.PROCESS_RECEIPT, receipt_id, max_attempts=UPLOAD_JOB_ATTEMPTS)
            uow.receipts.update_status(receipt_id, ReceiptStatus.PROCESSING, ProcessingStage.QUEUED)
            uow.commit()
        log.info("receipt.retried", receipt_id=receipt_id)
        return replace(
            receipt,
            status=ReceiptStatus.PROCESSING,
            stage=ProcessingStage.QUEUED,
            failure_reason=None,
        )


def failure_reason(error: BaseException) -> FailureReason:
    """Why a run that raised ``error`` failed, in words a person can act on. A refused, cut
    off or malformed answer is often the same on a second read, so it is not "Claude
    unavailable, try again in a minute" but UNKNOWN."""
    if isinstance(error, ProcessingStoppedError) and error.__cause__ is not None:
        error = error.__cause__
    if isinstance(error, BudgetReachedError):
        return FailureReason.BUDGET_REACHED
    if isinstance(error, ReaderKeyError):
        return FailureReason.NO_API_KEY
    if isinstance(error, ReaderUnavailableError):
        return FailureReason.CLAUDE_UNAVAILABLE
    if isinstance(error, UnsupportedMediaError):
        return FailureReason.UNREADABLE_PHOTO
    return FailureReason.UNKNOWN


def new_receipt(image: StoredImage, now: datetime) -> Receipt:
    """The receipt of a stored photo before the graph has read it: PROCESSING, at READING."""
    return Receipt(
        id=None,
        status=ReceiptStatus.PROCESSING,
        stage=ProcessingStage.READING,
        store_name="",
        store_key="",
        purchased_on=None,
        currency=DEFAULT_CURRENCY,
        subtotal=None,
        tax=None,
        total=None,
        payment_last4=None,
        ocr_text=None,
        image_path=image.relative_path,
        items=(),
        created_at=now,
        updated_at=now,
        image_sha256=image.sha256,
    )


class ProcessReceiptFile:
    def __init__(  # noqa: PLR0913 - the ports it works with, and the optional reader check
        self,
        image_store: ImageStore,
        uow_factory: UnitOfWorkFactory,
        runner: ReceiptGraphRunner,
        tracer: Tracer,
        clock: Clock,
        *,
        reader_check: Callable[[], None] | None = None,
    ) -> None:
        """``reader_check`` raises a ``ReaderError`` when no read could be made now (no key,
        the budget reached), without making one: ``reprocess`` asks it before it changes the
        receipt."""
        self._image_store = image_store
        self._uow_factory = uow_factory
        self._runner = runner
        self._tracer = tracer
        self._clock = clock
        self._reader_check = reader_check

    def __call__(
        self,
        source: ReceiptSource,
        *,
        reprocess: bool = False,
        interrupt_after: str | None = None,
    ) -> ProcessOutcome:
        """Process ``source``. UnsupportedMediaError (before any receipt exists) for an image
        that is not one; ProcessingStoppedError (a ReaderError) when a Claude read stops the
        run, with the receipt FAILED and its checkpoint kept. Any other failure marks the
        receipt FAILED and is returned, so a batch can go on.

        An image already stored is a duplicate, unless ``reprocess``: then Claude reads its
        stored photo again on a new thread, and its OPEN review is dismissed. With
        ``interrupt_after`` the run pauses after that node, its checkpoint kept."""
        image = self._image_store.save(source.image)
        existing = self._find(image.sha256)
        if existing is not None and not reprocess:
            return ProcessOutcome(receipt_id=require_id(existing.id), duplicate=True, graph=None)
        if existing is not None:
            return self._reprocess(existing, source.name, interrupt_after)
        try:
            receipt_id = self._create(image)
        except ConflictError:
            # An upload created the receipt of this photo since ``_find``.
            existing = self._find(image.sha256)
            if existing is None:
                raise
            return ProcessOutcome(receipt_id=require_id(existing.id), duplicate=True, graph=None)
        return self._run(
            receipt_id,
            lambda: self._runner.start(
                receipt_id,
                source_name=source.name,
                image_path=image.relative_path,
                interrupt_after=interrupt_after,
            ),
        )

    def resume(self, receipt_id: int) -> ProcessOutcome:
        """Continue the receipt's thread from its last checkpoint (after a pause, a stopped
        read or a crash); failures as for ``__call__``. NotFoundError for an unknown receipt;
        ConflictError when its thread waits for its OPEN review (``review resolve`` answers
        that; a FAILED receipt is set NEEDS_REVIEW again first, as a retry does) or has nothing
        to resume. A receipt marked FAILED by a crash after its matches were saved is
        COMPLETED again when the resumed run finishes, closing a review the run had taken."""
        with self._uow_factory(read_only=True) as uow:
            receipt = uow.receipts.get(receipt_id)
        if receipt is None:
            msg = f"receipt {receipt_id} does not exist"
            raise NotFoundError(msg)
        review = self._waits_for_review(receipt_id)
        if review is not None:
            msg = (
                f"receipt {receipt_id} waits for review #{review.id}:"
                f" answer it with `paper-trail review resolve {review.id}`"
            )
            raise ConflictError(msg)
        outcome = self._run(receipt_id, lambda: self._runner.resume(receipt_id))
        if outcome.graph is not None and outcome.graph.status is ReceiptStatus.COMPLETED:
            self._completed(receipt_id)
        return outcome

    def process_stored(self, receipt: Receipt) -> ProcessOutcome:
        """Run the graph over the stored photo of a receipt ``UploadReceipt`` created; when a
        run of it had started (a worker that died, a retried failure), continue its thread
        instead, unless an older version saved it: then the thread is forgotten and the photo
        read again. A thread still waiting for its OPEN review (an answer whose workers died
        before the graph took it) is not resumed, which would accept the reading: the receipt
        waits for a person again. A run that completes closes a review still OPEN (the answer
        was taken before the workers died), keeping a forced answer's reason. A fresh read
        dismisses an OPEN review first: the thread it belonged to is gone. Failures as for
        ``__call__``; ConflictError for a receipt without a stored photo."""
        receipt_id = require_id(receipt.id)
        image_path = receipt.image_path
        if image_path is None:
            msg = f"receipt {receipt_id} has no stored photo to read"
            raise ConflictError(msg)
        if self._waits_for_review(receipt_id) is not None:
            return ProcessOutcome(receipt_id=receipt_id, duplicate=False, graph=None)
        try:
            outcome = self._run(receipt_id, lambda: self._runner.resume(receipt_id))
        except NothingToResumeError:
            pass
        except OutdatedCheckpointError:
            self._runner.reset(receipt_id)
        else:
            if outcome.graph is not None and outcome.graph.status is ReceiptStatus.COMPLETED:
                self._completed(receipt_id)
            return outcome
        self._dismiss_open_review(receipt_id)
        return self._run(
            receipt_id,
            lambda: self._runner.start(
                receipt_id,
                source_name=PurePosixPath(image_path).name,
                image_path=image_path,
            ),
        )

    def _reprocess(
        self, receipt: Receipt, source_name: str, interrupt_after: str | None
    ) -> ProcessOutcome:
        """A new thread for a stored receipt: Claude reads its stored photo again. A receipt
        without one cannot be read again (ConflictError). The key and the budget are checked
        first (``reader_check``): a ProcessingStoppedError then leaves the receipt as it was.
        When the new run fails before anything is saved (a stopped read, a corrupt stored
        photo), the receipt gets its previous status back (FAILED instead when that was a
        review this run dismissed)."""
        receipt_id = require_id(receipt.id)
        image_path = receipt.image_path
        if image_path is None:
            msg = f"receipt {receipt_id} has no stored photo to read again"
            raise ConflictError(msg)
        if self._reader_check is not None:
            try:
                self._reader_check()
            except ReaderError as error:
                raise ProcessingStoppedError(receipt_id, error) from error
        previous: tuple[ReceiptStatus, ProcessingStage] | None = (receipt.status, receipt.stage)
        with self._uow_factory() as uow:
            review = uow.reviews.find_open(receipt_id)
            if review is not None:
                uow.reviews.dismiss(require_id(review.id))
                previous = None
            uow.receipts.update_status(
                receipt_id, ReceiptStatus.PROCESSING, ProcessingStage.READING
            )
            uow.commit()
        self._runner.reset(receipt_id)
        log.info("receipt.reprocessing", receipt_id=receipt_id)
        return self._run(
            receipt_id,
            lambda: self._runner.start(
                receipt_id,
                source_name=source_name,
                image_path=image_path,
                interrupt_after=interrupt_after,
            ),
            restore=previous,
        )

    def _run(
        self,
        receipt_id: int,
        run: Callable[[], GraphOutcome],
        *,
        restore: tuple[ReceiptStatus, ProcessingStage] | None = None,
    ) -> ProcessOutcome:
        """``run`` the graph. A ReaderError fails the receipt (keeping its checkpoint) and
        stops the batch; any other failure fails it and is returned. With ``restore``, a
        receipt the failed run had not saved yet (still at stage READING, where ``reprocess``
        put it) gets that status and stage back instead."""
        try:
            graph = run()
        except (NothingToResumeError, OutdatedCheckpointError):
            raise
        except ReaderError as error:
            self._settle_failure(receipt_id, error, restore)
            raise ProcessingStoppedError(receipt_id, error) from error
        except Exception as error:  # one receipt's failure must not stop a batch
            return self._settle_failure(receipt_id, error, restore)
        return ProcessOutcome(receipt_id=receipt_id, duplicate=False, graph=graph)

    def _settle_failure(
        self,
        receipt_id: int,
        error: Exception,
        restore: tuple[ReceiptStatus, ProcessingStage] | None,
    ) -> ProcessOutcome:
        unsaved = restore is not None and self._unsaved(receipt_id)
        outcome = self._failed(receipt_id, error)
        if unsaved and restore is not None:
            status, stage = restore
            reason = failure_reason(error) if status is ReceiptStatus.FAILED else None
            self._restore(receipt_id, status, stage, reason)
        return outcome

    def _unsaved(self, receipt_id: int) -> bool:
        """Whether the run has not saved the receipt yet (nor parked it for review)."""
        with self._uow_factory(read_only=True) as uow:
            receipt = uow.receipts.get(receipt_id)
        return receipt is not None and receipt.stage is ProcessingStage.READING

    def _restore(
        self,
        receipt_id: int,
        status: ReceiptStatus,
        stage: ProcessingStage,
        reason: FailureReason | None,
    ) -> None:
        """The status and stage a receipt had before a run that failed before saving it."""
        with self._uow_factory() as uow:
            uow.receipts.update_status(receipt_id, status, stage, failure_reason=reason)
            uow.commit()

    def _find(self, sha256: str) -> Receipt | None:
        with self._uow_factory(read_only=True) as uow:
            return uow.receipts.find_by_image_sha256(sha256)

    def _create(self, image: StoredImage) -> int:
        with self._uow_factory() as uow:
            receipt = uow.receipts.add(new_receipt(image, self._clock.now()))
            uow.commit()
        receipt_id = require_id(receipt.id)
        log.info("receipt.created", receipt_id=receipt_id)
        return receipt_id

    def _waits_for_review(self, receipt_id: int) -> ReviewItem | None:
        """The receipt's OPEN review when its thread still waits for it, else None. A FAILED or
        PROCESSING receipt is set NEEDS_REVIEW again, only while, read again in the write unit
        of work, the review is still OPEN and the receipt still FAILED or PROCESSING."""
        with self._uow_factory(read_only=True) as uow:
            review = uow.reviews.find_open(receipt_id)
        if review is None or not self._runner.waiting_for_review(receipt_id):
            return None
        review_id = require_id(review.id)
        with self._uow_factory() as uow:
            current = uow.reviews.get(review_id)
            receipt = uow.receipts.get(receipt_id)
            if (
                current is not None
                and current.status is ReviewStatus.OPEN
                and receipt is not None
                and receipt.status in {ReceiptStatus.FAILED, ReceiptStatus.PROCESSING}
            ):
                uow.receipts.update_status(
                    receipt_id, ReceiptStatus.NEEDS_REVIEW, ProcessingStage.NEEDS_REVIEW
                )
                uow.commit()
                log.info("receipt.waits_for_review", receipt_id=receipt_id, review_id=review_id)
        return review

    def _dismiss_open_review(self, receipt_id: int) -> None:
        with self._uow_factory() as uow:
            review = uow.reviews.find_open(receipt_id)
            if review is not None:
                uow.reviews.dismiss(require_id(review.id))
                uow.commit()

    def _completed(self, receipt_id: int) -> None:
        """A run that failed after its matches were saved (describing its new products) marked
        the receipt FAILED (and a retry PROCESSING); finishing that run completes the receipt
        again, and closes a review still OPEN, whose answer the run took."""
        with self._uow_factory() as uow:
            receipt = uow.receipts.get(receipt_id)
            if receipt is not None and receipt.status in {
                ReceiptStatus.FAILED,
                ReceiptStatus.PROCESSING,
            }:
                uow.receipts.update_status(
                    receipt_id, ReceiptStatus.COMPLETED, ProcessingStage.DONE
                )
            review = uow.reviews.find_open(receipt_id)
            if receipt is not None and review is not None:
                remember_forced_reason(
                    uow, receipt_id, _forced_reason(uow, review), self._clock.now()
                )
                uow.reviews.resolve(require_id(review.id))
            uow.commit()

    def _failed(self, receipt_id: int, error: Exception) -> ProcessOutcome:
        # The message may quote what a model read: card numbers masked before it is kept.
        failure = mask_pans(str(error)).text or type(error).__name__
        with self._uow_factory() as uow:
            uow.receipts.update_status(
                receipt_id,
                ReceiptStatus.FAILED,
                ProcessingStage.FAILED,
                failure_reason=failure_reason(error),
            )
            uow.commit()
        with self._tracer.bind(receipt_id):
            self._tracer.emit(
                AgentRole.SUPERVISOR,
                TraceKind.ERROR,
                type(error).__name__,
                failure,
                data={"error": failure},
            )
        # The message may quote receipt content (ADR-0011): only its type is logged above DEBUG.
        log.error("receipt.failed", receipt_id=receipt_id, error_type=type(error).__name__)
        log.debug("receipt.failed_detail", receipt_id=receipt_id, error=failure)
        return ProcessOutcome(receipt_id=receipt_id, duplicate=False, graph=None, failure=failure)


def _forced_reason(uow: UnitOfWork, review: ReviewItem) -> str | None:
    """The reason of a forced answer to ``review`` queued in the receipt's latest answer job."""
    job = uow.jobs.latest(JobKind.RESOLVE_REVIEW, review.receipt_id)
    if job is None:
        return None
    answer = ReviewAnswerPayload.model_validate(job.payload)
    return answer.reason if answer.force and answer.review_id == review.id else None
