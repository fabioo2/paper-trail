"""Running the receipt graph (agent-core plan 2.2): what the use cases see of the agents."""

from dataclasses import dataclass
from typing import Protocol

from paper_trail.domain.enums import ReceiptStatus, ReviewReason
from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import ReceiptDraft


class NothingToResumeError(ConflictError):
    """The receipt has no graph thread, or its thread has finished."""


class OutdatedCheckpointError(ConflictError):
    """The receipt's thread was saved by an older version whose state this one cannot read
    (``state_version`` below the current one); the message names the ways out: ``review
    resolve`` with a corrected file, or ``process --reprocess``."""


@dataclass(frozen=True, slots=True)
class GraphOutcome:
    receipt_id: int
    status: ReceiptStatus
    review_id: int | None
    review_reason: ReviewReason | None
    attempts: int  # Claude reads made (extract model, then retry model)
    paused_at: str | None  # the node a static breakpoint stopped after (slice C); not a review
    resumed_from: str | None = None  # a continued thread's last completed node before this run


class ReceiptGraphRunner(Protocol):
    """One graph thread per receipt (``receipt-<id>``), checkpointed after every node."""

    def start(
        self,
        receipt_id: int,
        *,
        source_name: str,
        image_path: str,
        interrupt_after: str | None = None,
    ) -> GraphOutcome:
        """Run a new thread over the stored photo ``image_path``. With ``interrupt_after`` the
        run pauses after that node (``paused_at``), checkpoint kept.

        Raises a ``ReaderError`` (``ports.reader``) when a read stops the run (the checkpoint
        is kept).
        """
        ...

    def resume(self, receipt_id: int, draft: ReceiptDraft | None = None) -> GraphOutcome:
        """Continue the thread: at a review, ``draft`` is the person's answer (None accepts the
        pending extraction); after a pause or a run that raised, from its last checkpoint
        (``draft`` must be None, else ConflictError). NothingToResumeError when the receipt
        has no thread or its thread has finished; OutdatedCheckpointError, before any write,
        when it is not waiting for a review and its saved state is from an older version."""
        ...

    def reset(self, receipt_id: int) -> None:
        """Forget the receipt's thread."""
        ...

    def pending_draft(self, receipt_id: int) -> ReceiptDraft | None:
        """The extraction a thread paused for review holds, as a draft; None if there is none,
        or if the thread was saved by an older version (only a person's draft answers it)."""
        ...
