"""Processing one receipt file: store it, create the receipt, run the graph (agent-core plan 2.2).

An image is stored once by content: the same photo again returns the receipt it already made.
A text (OCR output from a file) is masked before anything else and never deduplicated. Either
way the receipt exists, PROCESSING, before the graph starts, so a run that stops half way
(the LLM host went away: WAITING_FOR_GPU) leaves something to resume (``resume``). A stored
photo can be run again on a new thread (``reprocess``).
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
from paper_trail.ports.agents import GraphOutcome, NothingToResumeError, ReceiptGraphRunner
from paper_trail.ports.clock import Clock
from paper_trail.ports.images import ImageStore, StoredImage
from paper_trail.ports.llm import LlmUnavailableError
from paper_trail.ports.trace import Tracer
from paper_trail.ports.unit_of_work import UnitOfWorkFactory

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReceiptSource:
    """A receipt to process: a photo, or OCR text. Exactly one of the two."""

    name: str  # shown in the trace, e.g. the file name
    image: bytes | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        if (self.image is None) == (self.text is None):
            msg = f"{self.name}: a receipt source is either an image or a text"
            raise ValueError(msg)


class ProcessingStoppedError(LlmUnavailableError):
    """The LLM host went away while ``receipt_id`` was being processed; the receipt stays
    PROCESSING with its checkpoint, to be resumed."""

    def __init__(self, receipt_id: int, cause: LlmUnavailableError) -> None:
        super().__init__(str(cause))
        self.receipt_id = receipt_id


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    receipt_id: int
    duplicate: bool  # the image was already processed: no new run
    graph: GraphOutcome | None  # None for a duplicate or a failed run
    failure: str | None = None  # why the run failed (the receipt is FAILED)


class ProcessReceiptFile:
    def __init__(
        self,
        image_store: ImageStore,
        uow_factory: UnitOfWorkFactory,
        runner: ReceiptGraphRunner,
        tracer: Tracer,
        clock: Clock,
    ) -> None:
        self._image_store = image_store
        self._uow_factory = uow_factory
        self._runner = runner
        self._tracer = tracer
        self._clock = clock

    def __call__(
        self,
        source: ReceiptSource,
        *,
        reprocess: bool = False,
        interrupt_after: str | None = None,
    ) -> ProcessOutcome:
        """Process ``source``. UnsupportedMediaError (before any receipt exists) for an image
        that is not one; ProcessingStoppedError (an LlmUnavailableError) when the LLM host is
        unreachable, with the receipt left PROCESSING and WAITING_FOR_GPU. Any other failure
        marks the receipt FAILED and is returned, so a batch can go on.

        An image already stored is a duplicate, unless ``reprocess``: then its receipt runs
        again on a new thread from its stored OCR text, and its OPEN review is dismissed. With
        ``interrupt_after`` the run pauses after that node, its checkpoint kept."""
        image: StoredImage | None = None
        ocr_text: str | None = None
        if source.image is not None:
            image = self._image_store.save(source.image)
            existing = self._find(image.sha256)
            if existing is not None and not reprocess:
                return ProcessOutcome(
                    receipt_id=require_id(existing.id), duplicate=True, graph=None
                )
            if existing is not None:
                return self._reprocess(existing, source.name, interrupt_after)
        else:
            ocr_text = mask_pans(source.text or "").text
        receipt_id = self._create(image, ocr_text)
        return self._run(
            receipt_id,
            lambda: self._runner.start(
                receipt_id,
                source_name=source.name,
                image_path=None if image is None else image.relative_path,
                ocr_text=ocr_text,
                interrupt_after=interrupt_after,
            ),
        )

    def resume(self, receipt_id: int) -> ProcessOutcome:
        """Continue the receipt's thread from its last checkpoint (after a pause, a GPU drop
        or a crash); failures as for ``__call__``. NotFoundError for an unknown receipt;
        ConflictError when it waits for a review (``review resolve`` answers that) or has
        nothing to resume."""
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
        return self._run(receipt_id, lambda: self._runner.resume(receipt_id))

    def _reprocess(
        self, receipt: Receipt, source_name: str, interrupt_after: str | None
    ) -> ProcessOutcome:
        """A new thread for a stored receipt, from its OCR text (from its image if none)."""
        receipt_id = require_id(receipt.id)
        with self._uow_factory() as uow:
            review = uow.reviews.find_open(receipt_id)
            if review is not None:
                uow.reviews.dismiss(require_id(review.id))
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
                image_path=receipt.image_path,
                ocr_text=receipt.ocr_text,
                interrupt_after=interrupt_after,
            ),
        )

    def _run(self, receipt_id: int, run: Callable[[], GraphOutcome]) -> ProcessOutcome:
        try:
            graph = run()
        except NothingToResumeError:
            raise
        except LlmUnavailableError as error:
            self._wait_for_gpu(receipt_id)
            raise ProcessingStoppedError(receipt_id, error) from error
        except Exception as error:  # one receipt's failure must not stop a batch
            return self._failed(receipt_id, error)
        return ProcessOutcome(receipt_id=receipt_id, duplicate=False, graph=graph)

    def _find(self, sha256: str) -> Receipt | None:
        with self._uow_factory(read_only=True) as uow:
            return uow.receipts.find_by_image_sha256(sha256)

    def _wait_for_gpu(self, receipt_id: int) -> None:
        """S3-AC-10: a receipt not yet saved waits for the GPU; a saved one keeps its state."""
        with self._uow_factory() as uow:
            receipt = uow.receipts.get(receipt_id)
            if receipt is not None and receipt.status is ReceiptStatus.PROCESSING:
                uow.receipts.update_status(
                    receipt_id, ReceiptStatus.PROCESSING, ProcessingStage.WAITING_FOR_GPU
                )
                uow.commit()

    def _create(self, image: StoredImage | None, ocr_text: str | None) -> int:
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
                    ocr_text=ocr_text,
                    image_path=None if image is None else image.relative_path,
                    items=(),
                    created_at=now,
                    updated_at=now,
                    image_sha256=None if image is None else image.sha256,
                )
            )
            uow.commit()
        receipt_id = require_id(receipt.id)
        log.info("receipt.created", receipt_id=receipt_id, from_image=image is not None)
        return receipt_id

    def _failed(self, receipt_id: int, error: Exception) -> ProcessOutcome:
        failure = str(error) or type(error).__name__
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
