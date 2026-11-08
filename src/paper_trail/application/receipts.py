"""Recording receipts, resolving their reviews, and reading them back (PLAN 3.6, PLAN 5)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

import structlog

from paper_trail.application.matching import (
    MatchChoice,
    MatchesRecorded,
    plan_matches,
    record_matches,
)
from paper_trail.application.payloads import IssuePayload
from paper_trail.application.products import new_product, price_observation, remember_alias
from paper_trail.domain.enums import (
    AliasSource,
    CorrectionKind,
    LineKind,
    ProcessingStage,
    ReceiptStatus,
    ReviewReason,
    ReviewStatus,
)
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import (
    Correction,
    JsonValue,
    LineItem,
    PriceObservation,
    Receipt,
    ReceiptDraft,
    ReceiptSummary,
    ReviewItem,
    require_id,
)
from paper_trail.domain.money import Money
from paper_trail.domain.paging import MAX_PAGE_LIMIT, Page, PageRequest
from paper_trail.domain.rules.arithmetic import (
    ArithmeticReport,
    Tolerances,
    check_arithmetic,
)
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.domain.rules.normalization import normalize_alias, normalize_store_name
from paper_trail.ports.agents import ReceiptGraphRunner
from paper_trail.ports.clock import Clock
from paper_trail.ports.embeddings import EmbeddingPort
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReceiptAssessment:
    """What recording a draft would do, decided before anything is written."""

    draft: ReceiptDraft  # with card numbers masked
    masked_pans: int
    arithmetic: ArithmeticReport
    status: ReceiptStatus
    stage: ProcessingStage


@dataclass(frozen=True, slots=True)
class RecordReceiptResult:
    receipt: Receipt
    arithmetic: ArithmeticReport
    review: ReviewItem | None
    masked_pans: int


class RecordReceipt:
    """Store a receipt draft: masked, checked, linked to products, priced, or sent to review.

    A draft that adds up is COMPLETED and, when its purchase date is known, yields one price
    observation per linked ITEM line. One that does not is NEEDS_REVIEW with an OPEN
    VALIDATION_FAILED review item listing the issues. A draft that mixes currencies is such a
    draft (CURRENCY_MISMATCH issues): its foreign amounts are stored as the same cents in the
    receipt's currency, and the review keeps what was read. Everything happens in one unit of
    work.
    """

    def __init__(
        self, uow_factory: UnitOfWorkFactory, clock: Clock, tolerances: Tolerances
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._tolerances = tolerances

    def assess(self, draft: ReceiptDraft) -> ReceiptAssessment:
        """Mask and check ``draft`` without writing anything (``receipts import --dry-run``)."""
        masked, masked_pans = _masked(draft)
        report = check_arithmetic(masked, self._tolerances)
        if report.ok:
            status, stage = ReceiptStatus.COMPLETED, ProcessingStage.DONE
        else:
            status, stage = ReceiptStatus.NEEDS_REVIEW, ProcessingStage.NEEDS_REVIEW
        return ReceiptAssessment(masked, masked_pans, report, status, stage)

    def __call__(self, draft: ReceiptDraft) -> RecordReceiptResult:
        assessment = self.assess(draft)
        now = self._clock.now()
        with self._uow_factory() as uow:
            receipt = uow.receipts.add(
                self._receipt(uow, assessment, assessment.status, assessment.stage, now)
            )
            receipt_id = require_id(receipt.id)
            review = None
            if not assessment.arithmetic.ok:
                review = uow.reviews.open(
                    receipt_id,
                    ReviewReason.VALIDATION_FAILED,
                    review_details(assessment.arithmetic),
                    thread_id=None,
                )
            _record_prices(uow, receipt, assessment.arithmetic)
            uow.commit()
        return self._recorded(receipt, assessment, review)

    def replace(
        self, receipt_id: int, draft: ReceiptDraft, *, force: bool = False, matching: bool = False
    ) -> RecordReceiptResult:
        """Record ``draft`` as the contents of an existing receipt (the graph's save step).

        Like recording, but ``created_at`` and the image are kept, and so are the OCR text and
        image path when the draft has none. The receipt is accepted when the draft adds up or
        ``force`` is set (a person approved it), else NEEDS_REVIEW; no review item is opened
        (the graph opens its own). An accepted receipt is COMPLETED, or with ``matching`` set
        PROCESSING/MATCHING until ``record_matches`` has linked its lines. NotFoundError for an
        unknown receipt.
        """
        assessment = self.assess(draft)
        if (assessment.arithmetic.ok or force) and matching:
            status, stage = ReceiptStatus.PROCESSING, ProcessingStage.MATCHING
        elif assessment.arithmetic.ok or force:
            status, stage = ReceiptStatus.COMPLETED, ProcessingStage.DONE
        else:
            status, stage = ReceiptStatus.NEEDS_REVIEW, ProcessingStage.NEEDS_REVIEW
        now = self._clock.now()
        with self._uow_factory() as uow:
            current = uow.receipts.get(receipt_id)
            if current is None:
                msg = f"receipt {receipt_id} does not exist"
                raise NotFoundError(msg)
            contents = self._receipt(uow, assessment, status, stage, now)
            receipt = uow.receipts.replace_contents(
                replace(
                    contents,
                    id=receipt_id,
                    created_at=current.created_at,
                    image_sha256=current.image_sha256,
                    ocr_text=_first_known(contents.ocr_text, current.ocr_text),
                    image_path=_first_known(contents.image_path, current.image_path),
                )
            )
            _record_prices(uow, receipt, assessment.arithmetic)
            uow.commit()
        return self._recorded(receipt, assessment, None)

    def record_matches(
        self, receipt_id: int, choices: Sequence[MatchChoice], embeddings: EmbeddingPort
    ) -> MatchesRecorded:
        """Link a saved receipt's lines to their products (the ``save_matches`` tool).

        The aliases of the new products are embedded first, in one call and outside any
        transaction (a slow or absent embedding host must not hold the write lock), then
        everything is written in one unit of work. A failure writes nothing.
        """
        with self._uow_factory(read_only=True) as uow:
            plan = plan_matches(uow, receipt_id, choices)
        aliases = plan.aliases_to_embed
        vectors = dict(zip(aliases, embeddings.embed(aliases), strict=True))
        with self._uow_factory() as uow:
            recorded = record_matches(
                uow, plan_matches(uow, receipt_id, choices), choices, vectors, self._clock.now()
            )
            uow.commit()
        log.info(
            "receipt.matched",
            receipt_id=receipt_id,
            linked=len(recorded.links),
            new_products=recorded.new_products,
        )
        return recorded

    def _receipt(
        self,
        uow: UnitOfWork,
        assessment: ReceiptAssessment,
        status: ReceiptStatus,
        stage: ProcessingStage,
        now: datetime,
    ) -> Receipt:
        """The unsaved receipt of a masked draft, its lines linked to their named products."""
        masked = assessment.draft
        store_key = normalize_store_name(masked.store_name)
        currency = masked.currency
        items = tuple(
            _in_currency(
                _linked(uow, item, masked.product_names.get(item.position), store_key, now),
                currency,
            )
            for item in masked.items
        )
        return Receipt(
            id=None,
            status=status,
            stage=stage,
            store_name=masked.store_name,
            store_key=store_key,
            purchased_on=masked.purchased_on,
            currency=currency,
            subtotal=_amount_in(masked.subtotal, currency),
            tax=_amount_in(masked.tax, currency),
            total=_amount_in(masked.total, currency),
            payment_last4=masked.payment_last4,
            ocr_text=masked.ocr_text,
            image_path=masked.image_path,
            items=items,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _recorded(
        receipt: Receipt, assessment: ReceiptAssessment, review: ReviewItem | None
    ) -> RecordReceiptResult:
        log.info(
            "receipt.recorded",
            receipt_id=receipt.id,
            status=receipt.status.value,
            item_count=len(receipt.items),
            masked_pans=assessment.masked_pans,
        )
        return RecordReceiptResult(receipt, assessment.arithmetic, review, assessment.masked_pans)


class InconsistentDraftError(DomainValidationError):
    """A person's draft does not add up and was not forced through; ``report`` says why."""

    def __init__(self, report: ArithmeticReport) -> None:
        super().__init__("the receipt does not add up: " + "; ".join(_messages(report)))
        self.report = report


class ResolveReview:
    """A person's answer to a review: it resumes the receipt's parked graph thread (S3-AC-07).

    The answer is a corrected draft, or (``draft=None``) the extraction the graph holds. It
    must add up unless ``force`` is set with a reason, which is remembered as a correction.
    The graph then saves the draft as PROCESSING/MATCHING, ``save_matches`` completes it, and only
    after that is the review RESOLVED.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        runner: ReceiptGraphRunner,
        record_receipt: RecordReceipt,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._runner = runner
        self._record_receipt = record_receipt
        self._clock = clock

    def __call__(
        self,
        review_id: int,
        draft: ReceiptDraft | None = None,
        *,
        force: bool = False,
        reason: str | None = None,
    ) -> Receipt:
        review = self._open_review(review_id)
        receipt_id = review.receipt_id
        if review.thread_id is None:
            msg = f"review {review_id} was not opened by the agents: there is no run to resume"
            raise DomainValidationError(msg)
        if force and not (reason and reason.strip()):
            msg = "forcing a draft that does not add up needs a reason"
            raise DomainValidationError(msg)
        if draft is None:
            draft = self._runner.pending_draft(receipt_id)
        if draft is None:
            msg = "there is no extraction to accept; resolve with a corrected draft (--file)"
            raise DomainValidationError(msg)
        assessment = self._record_receipt.assess(draft)
        overridden = not assessment.arithmetic.ok
        if overridden and not force:
            raise InconsistentDraftError(assessment.arithmetic)
        self._runner.resume(receipt_id, assessment.draft)  # masked before it enters the graph
        with self._uow_factory() as uow:
            if overridden and reason is not None:
                uow.corrections.add(
                    Correction(
                        id=None,
                        kind=CorrectionKind.FIELD,
                        subject=f"receipt:{receipt_id}.arithmetic",
                        old_value=None,
                        new_value=reason.strip(),
                        store_key=None,
                        receipt_id=receipt_id,
                        line_item_id=None,
                        created_at=self._clock.now(),
                    )
                )
            uow.reviews.resolve(review_id)
            receipt = uow.receipts.get(receipt_id)
            uow.commit()
        if receipt is None:
            msg = f"receipt {receipt_id} does not exist"
            raise NotFoundError(msg)
        log.info("review.resolved", review_id=review_id, receipt_id=receipt_id, forced=overridden)
        return receipt

    def _open_review(self, review_id: int) -> ReviewItem:
        review = GetReview(self._uow_factory)(review_id)
        if review.status is not ReviewStatus.OPEN:
            msg = f"review {review_id} is {review.status.value}, not open"
            raise ConflictError(msg)
        return review


class ListOpenReviews:
    """Every OPEN review item, newest first."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(self) -> tuple[ReviewItem, ...]:
        found: list[ReviewItem] = []
        request = PageRequest(limit=MAX_PAGE_LIMIT)
        with self._uow_factory(read_only=True) as uow:
            while True:
                page = uow.reviews.list_open(request)
                found += page.items
                if page.next_cursor is None:
                    return tuple(found)
                request = PageRequest(limit=MAX_PAGE_LIMIT, cursor=page.next_cursor)


class GetReview:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(self, review_id: int) -> ReviewItem:
        with self._uow_factory(read_only=True) as uow:
            review = uow.reviews.get(review_id)
        if review is None:
            msg = f"review {review_id} does not exist"
            raise NotFoundError(msg)
        return review


class GetReceipt:
    """A receipt and its arithmetic report, computed from the data as it is now."""

    def __init__(self, uow_factory: UnitOfWorkFactory, tolerances: Tolerances) -> None:
        self._uow_factory = uow_factory
        self._tolerances = tolerances

    def __call__(self, receipt_id: int) -> tuple[Receipt, ArithmeticReport]:
        with self._uow_factory(read_only=True) as uow:
            receipt = uow.receipts.get(receipt_id)
        if receipt is None:
            msg = f"receipt {receipt_id} does not exist"
            raise NotFoundError(msg)
        return receipt, check_arithmetic(receipt, self._tolerances)


class ListReceipts:
    """Receipts newest first, optionally of one status and one store (any spelling of it)."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(
        self, page: PageRequest, status: ReceiptStatus | None, store: str | None
    ) -> Page[ReceiptSummary]:
        store_key = None if store is None else normalize_store_name(store)
        with self._uow_factory(read_only=True) as uow:
            return uow.receipts.list(page, status=status, store_key=store_key)


def _masked(draft: ReceiptDraft) -> tuple[ReceiptDraft, int]:
    """``draft`` with card numbers masked in every free text, and how many were masked.

    Free text is anything a person or OCR wrote: the store name, the OCR text, each line's raw
    text and description, and the product names (which become products, aliases and FTS rows).
    """
    count = 0

    def masked(text: str) -> str:
        nonlocal count
        result = mask_pans(text)
        count += result.masked_count
        return result.text

    cleaned = replace(
        draft,
        store_name=masked(draft.store_name),
        ocr_text=None if draft.ocr_text is None else masked(draft.ocr_text),
        items=tuple(
            replace(item, raw_text=masked(item.raw_text), description=masked(item.description))
            for item in draft.items
        ),
        product_names={position: masked(name) for position, name in draft.product_names.items()},
    )
    return cleaned, count


def _linked(
    uow: UnitOfWork, line: LineItem, product_name: str | None, store_key: str, now: datetime
) -> LineItem:
    """``line`` linked to the product named ``product_name`` (created on first sight).

    The product is found through its global alias; the line's raw text is remembered as an
    alias for this store, so the store's spelling links directly next time. A name that
    normalizes to nothing names no product.
    """
    if product_name is None:
        return line
    name_alias = normalize_alias(product_name)
    if not name_alias:
        return line
    product = uow.products.find_by_alias(name_alias, None)
    if product is None:
        product = new_product(uow, product_name, line.category, now)
        remember_alias(
            uow,
            require_id(product.id),
            name_alias,
            store_key=None,
            source=AliasSource.IMPORT,
            now=now,
        )
    product_id = require_id(product.id)
    store_alias = normalize_alias(line.raw_text)
    if store_alias:
        remember_alias(
            uow, product_id, store_alias, store_key=store_key, source=AliasSource.IMPORT, now=now
        )
    return replace(line, product_id=product_id)


def _first_known(value: str | None, fallback: str | None) -> str | None:
    return fallback if value is None else value


def _amount_in(money: Money | None, currency: str) -> Money | None:
    """``money`` as an amount of ``currency``: a receipt holds one currency, so a draft's
    foreign amount (reported as a CURRENCY_MISMATCH issue) is stored as the same cents."""
    return None if money is None else Money(money.amount_cents, currency)


def _in_currency(line: LineItem, currency: str) -> LineItem:
    return replace(
        line,
        unit_price=_amount_in(line.unit_price, currency),
        line_total=Money(line.line_total.amount_cents, currency),
    )


def _record_prices(uow: UnitOfWork, receipt: Receipt, report: ArithmeticReport) -> None:
    """Price observations, only for a receipt that adds up and whose date is known."""
    if report.ok and receipt.purchased_on is not None:
        uow.prices.add_many(_observations(receipt, receipt.purchased_on))


def _observations(receipt: Receipt, purchased_on: date) -> list[PriceObservation]:
    """One observation per ITEM line linked to a product."""
    return [
        price_observation(receipt, item, purchased_on)
        for item in receipt.items
        if item.kind is LineKind.ITEM and item.product_id is not None
    ]


def _messages(report: ArithmeticReport) -> list[str]:
    return [issue.message for issue in report.issues]


def review_details(report: ArithmeticReport) -> Mapping[str, JsonValue]:
    """A review item's ``details``: the arithmetic issues (the shape the API and CLI show)."""
    return {"issues": [IssuePayload.of(issue).model_dump(mode="json") for issue in report.issues]}
