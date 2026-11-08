"""Running the receipt graph (agent-core plan 2.2): what the use cases see of the agents."""

from dataclasses import dataclass
from typing import Protocol

from paper_trail.domain.enums import ReceiptStatus, ReviewReason
from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import ReceiptDraft


class NothingToResumeError(ConflictError):
    """The receipt has no graph thread, or its thread has finished."""


@dataclass(frozen=True, slots=True)
class GraphOutcome:
    receipt_id: int
    status: ReceiptStatus
    review_id: int | None
    review_reason: ReviewReason | None
    text_attempts: int
    vision_attempts: int
    paused_at: str | None  # the node a static breakpoint stopped after (slice C); not a review
    resumed_from: str | None = None  # a continued thread's last completed node before this run


class ReceiptGraphRunner(Protocol):
    """One graph thread per receipt (``receipt-<id>``), checkpointed after every node."""

    def start(
        self,
        receipt_id: int,
        *,
        source_name: str,
        image_path: str | None = None,
        ocr_text: str | None = None,
        interrupt_after: str | None = None,
    ) -> GraphOutcome:
        """Run a new thread. With ``ocr_text`` (already masked) the OCR step is skipped. With
        ``interrupt_after`` the run pauses after that node (``paused_at``), checkpoint kept.

        Raises LlmUnavailableError when the LLM host is down (the checkpoint is kept).
        """
        ...

    def resume(self, receipt_id: int, draft: ReceiptDraft | None = None) -> GraphOutcome:
        """Continue the thread: at a review, ``draft`` is the person's answer (None accepts the
        pending extraction); after a pause or a run that raised, from its last checkpoint
        (``draft`` must be None, else ConflictError). NothingToResumeError when the receipt
        has no thread or its thread has finished."""
        ...

    def reset(self, receipt_id: int) -> None:
        """Forget the receipt's thread."""
        ...

    def pending_draft(self, receipt_id: int) -> ReceiptDraft | None:
        """The extraction a thread paused for review holds, as a draft (None if there is none)."""
        ...
