"""Processing one receipt photo: store it, create the receipt, run the graph (agent-core plan
2.2, claude-extraction plan V7, V11).

An image is stored once by content: the same photo again returns the receipt it already made.
The receipt exists, PROCESSING, before the graph starts, so a run that a Claude read stops half
way (the API failed, refused, was cut off, or the monthly budget is reached) leaves the receipt
FAILED with its checkpoint, to resume (``resume``). A stored photo can be read again on a new
thread (``reprocess``).
"""

from collections.abc import Callable
from dataclasses import dataclass

import structlog

from paper_trail.domain.enums import AgentRole, ProcessingStage, ReceiptStatus
from paper_trail.domain.errors import ConflictError, NotFoundError
from paper_trail.domain.models import Receipt, require_id
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
from paper_trail.ports.reader import ReaderError
from paper_trail.ports.trace import Tracer
from paper_trail.ports.unit_of_work import UnitOfWorkFactory

log = structlog.get_logger(__name__)


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
        receipt_id = self._create(image)
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
        ConflictError when it waits for a review (``review resolve`` answers that) or has
        nothing to resume. A receipt marked FAILED by a crash after its matches were saved is
        COMPLETED again when the resumed run finishes."""
        with self._uow_factory(read_only=True) as uow:
            receipt = uow.receipts.get(receipt_id)
            review = uow.reviews.find_open(receipt_id)
        if receipt is None:
            msg = f"receipt {receipt_id} does not exist"
            raise NotFoundError(msg)
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
            self._restore(receipt_id, *restore)
        return outcome

    def _unsaved(self, receipt_id: int) -> bool:
        """Whether the run has not saved the receipt yet (nor parked it for review)."""
        with self._uow_factory(read_only=True) as uow:
            receipt = uow.receipts.get(receipt_id)
        return receipt is not None and receipt.stage is ProcessingStage.READING

    def _restore(self, receipt_id: int, status: ReceiptStatus, stage: ProcessingStage) -> None:
        """The status and stage a receipt had before a run that failed before saving it."""
        with self._uow_factory() as uow:
            uow.receipts.update_status(receipt_id, status, stage)
            uow.commit()

    def _find(self, sha256: str) -> Receipt | None:
        with self._uow_factory(read_only=True) as uow:
            return uow.receipts.find_by_image_sha256(sha256)

    def _create(self, image: StoredImage) -> int:
        now = self._clock.now()
        with self._uow_factory() as uow:
            receipt = uow.receipts.add(
                Receipt(
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
            )
            uow.commit()
        receipt_id = require_id(receipt.id)
        log.info("receipt.created", receipt_id=receipt_id)
        return receipt_id

    def _completed(self, receipt_id: int) -> None:
        """A run that failed after its matches were saved (describing its new products) marked
        the receipt FAILED; finishing that run completes the receipt again."""
        with self._uow_factory() as uow:
            receipt = uow.receipts.get(receipt_id)
            if receipt is not None and receipt.status is ReceiptStatus.FAILED:
                uow.receipts.update_status(
                    receipt_id, ReceiptStatus.COMPLETED, ProcessingStage.DONE
                )
                uow.commit()

    def _failed(self, receipt_id: int, error: Exception) -> ProcessOutcome:
        # The message may quote what a model read: card numbers masked before it is kept.
        failure = mask_pans(str(error)).text or type(error).__name__
        with self._uow_factory() as uow:
            uow.receipts.update_status(receipt_id, ReceiptStatus.FAILED, ProcessingStage.FAILED)
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
