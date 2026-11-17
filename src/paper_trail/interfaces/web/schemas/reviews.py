"""Bodies of the review endpoints: open reviews, one with the reading it holds, a person's
answer."""

from datetime import date, datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, StrictInt, model_validator

from paper_trail.application.payloads import CurrencyCode, IssuePayload, Last4, QuantityText
from paper_trail.application.receipts import PendingReview
from paper_trail.domain.enums import (
    Category,
    LineKind,
    ProcessingStage,
    ReceiptStatus,
    ReviewReason,
    ReviewStatus,
)
from paper_trail.domain.models import LineItem, Receipt, ReceiptDraft, ReviewItem, require_id
from paper_trail.domain.money import Money
from paper_trail.domain.rules.arithmetic import IssueCode
from paper_trail.interfaces.web.schemas.receipts import MoneyOut


class ReviewIssueOut(BaseModel):
    """Why the receipt waits for a person; ``position`` is the draft line it is about."""

    code: IssueCode
    message: str
    position: int | None


class ReviewLineBody(BaseModel):
    """One line of a reading. ``applies_to`` is the index, in ``lines``, of the item a discount
    belongs to."""

    model_config = ConfigDict(extra="forbid")

    kind: LineKind
    raw_text: str
    description: str
    quantity: QuantityText  # the exact decimal, e.g. "0.452"
    unit_price_cents: StrictInt | None
    line_total_cents: StrictInt
    category: Category | None
    applies_to: StrictInt | None

    @classmethod
    def from_line(cls, line: LineItem) -> Self:
        return cls(
            kind=line.kind,
            raw_text=line.raw_text,
            description=line.description,
            quantity=str(line.quantity),
            unit_price_cents=None if line.unit_price is None else line.unit_price.amount_cents,
            line_total_cents=line.line_total.amount_cents,
            category=line.category,
            applies_to=line.applies_to_position,
        )

    def to_line(self, position: int, currency: str) -> LineItem:
        return LineItem(
            position=position,
            kind=self.kind,
            raw_text=self.raw_text,
            description=self.description,
            quantity=Decimal(self.quantity),
            unit_price=_money(self.unit_price_cents, currency),
            line_total=Money(self.line_total_cents, currency),
            category=self.category,
            applies_to_position=self.applies_to,
        )


class ReviewDraftBody(BaseModel):
    """A reading of the receipt, as the review shows it and as a person corrects it: amounts in
    cents of ``currency``, lines in printed order."""

    model_config = ConfigDict(extra="forbid")

    store_name: str
    purchased_on: date | None
    currency: CurrencyCode
    subtotal_cents: StrictInt | None
    tax_cents: StrictInt | None
    total_cents: StrictInt
    payment_last4: Last4 | None
    lines: list[ReviewLineBody]

    @classmethod
    def from_draft(cls, draft: ReceiptDraft) -> Self:
        return cls(
            store_name=draft.store_name,
            purchased_on=draft.purchased_on,
            currency=draft.currency,
            subtotal_cents=None if draft.subtotal is None else draft.subtotal.amount_cents,
            tax_cents=None if draft.tax is None else draft.tax.amount_cents,
            total_cents=draft.total.amount_cents,
            payment_last4=draft.payment_last4,
            lines=[ReviewLineBody.from_line(line) for line in draft.items],
        )

    def to_draft(self) -> ReceiptDraft:
        """DomainValidationError for a discount linked to a line it cannot apply to."""
        currency = self.currency
        return ReceiptDraft(
            store_name=self.store_name,
            purchased_on=self.purchased_on,
            currency=currency,
            subtotal=_money(self.subtotal_cents, currency),
            tax=_money(self.tax_cents, currency),
            total=Money(self.total_cents, currency),
            items=tuple(
                line.to_line(position, currency) for position, line in enumerate(self.lines)
            ),
            payment_last4=self.payment_last4,
            ocr_text=None,
            image_path=None,
        )


class ReadingSummaryOut(BaseModel):
    store_name: str
    purchased_on: date | None
    total: MoneyOut
    item_count: int

    @classmethod
    def from_draft(cls, draft: ReceiptDraft) -> Self:
        return cls(
            store_name=draft.store_name,
            purchased_on=draft.purchased_on,
            total=MoneyOut.from_domain(draft.total),
            item_count=sum(1 for line in draft.items if line.kind is LineKind.ITEM),
        )


class ReviewSummaryOut(BaseModel):
    """An open review as listed; ``reading`` is the extraction it holds, null when there is
    none (the reading never parsed)."""

    id: int
    receipt_id: int
    reason: ReviewReason
    issues: list[ReviewIssueOut]
    created_at: datetime
    reading: ReadingSummaryOut | None

    @classmethod
    def from_pending(cls, pending: PendingReview) -> Self:
        review = pending.review
        return cls(
            id=require_id(review.id),
            receipt_id=review.receipt_id,
            reason=review.reason,
            issues=_issues(review),
            created_at=review.created_at,
            reading=None if pending.draft is None else ReadingSummaryOut.from_draft(pending.draft),
        )


class ReviewOut(BaseModel):
    """One review: ``draft`` is the extraction to accept or correct (null when there is none
    and the reading must be typed in); ``image_url`` serves the receipt's photo.
    ``answer_queued``: an answer is queued or running in the worker (answering again is a
    409)."""

    id: int
    receipt_id: int
    reason: ReviewReason
    status: ReviewStatus
    receipt_status: ReceiptStatus
    answer_queued: bool
    issues: list[ReviewIssueOut]
    created_at: datetime
    image_url: str | None
    draft: ReviewDraftBody | None

    @classmethod
    def from_pending(cls, pending: PendingReview) -> Self:
        review, receipt = pending.review, pending.receipt
        return cls(
            id=require_id(review.id),
            receipt_id=review.receipt_id,
            reason=review.reason,
            status=review.status,
            receipt_status=receipt.status,
            answer_queued=pending.answer_queued,
            issues=_issues(review),
            created_at=review.created_at,
            image_url=_image_url(receipt),
            draft=None if pending.draft is None else ReviewDraftBody.from_draft(pending.draft),
        )


class ResolveReviewIn(BaseModel):
    """Either ``accept`` (the extraction as read) or a corrected ``draft``. One that does not
    add up is refused unless ``force`` is set with a ``reason``."""

    model_config = ConfigDict(extra="forbid")

    accept: bool = False
    draft: ReviewDraftBody | None = None
    force: bool = False
    reason: str | None = None

    @model_validator(mode="after")
    def _one_answer(self) -> Self:
        if self.accept == (self.draft is not None):
            msg = "give either accept: true or a draft"
            raise ValueError(msg)
        return self


class ReviewAnswerOut(BaseModel):
    """The receipt once the answer is queued: the worker saves it (poll GET
    /api/receipts/{receipt_id})."""

    receipt_id: int
    status: ReceiptStatus
    stage: ProcessingStage

    @classmethod
    def from_receipt(cls, receipt: Receipt) -> Self:
        return cls(receipt_id=require_id(receipt.id), status=receipt.status, stage=receipt.stage)


def _issues(review: ReviewItem) -> list[ReviewIssueOut]:
    issues = review.details.get("issues")
    if not isinstance(issues, list):
        return []
    found = [IssuePayload.model_validate(issue) for issue in issues]
    return [
        ReviewIssueOut(code=issue.code, message=issue.message, position=issue.position)
        for issue in found
    ]


def _image_url(receipt: Receipt) -> str | None:
    if receipt.image_path is None:
        return None
    return f"/api/receipts/{require_id(receipt.id)}/image"


def _money(cents: int | None, currency: str) -> Money | None:
    return None if cents is None else Money(cents, currency)
