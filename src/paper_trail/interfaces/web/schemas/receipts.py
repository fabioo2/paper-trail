"""Response bodies of the receipt endpoints (PLAN 3.7); also printed by ``receipts show``."""

from collections.abc import Mapping
from datetime import date, datetime

from pydantic import BaseModel

from paper_trail.domain.enums import (
    Category,
    LineKind,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
)
from paper_trail.domain.models import LineItem, Product, Receipt, ReceiptSummary, require_id
from paper_trail.domain.money import Money
from paper_trail.domain.paging import Page
from paper_trail.domain.rules.arithmetic import ArithmeticIssue, ArithmeticReport, IssueCode


class MoneyOut(BaseModel):
    amount_cents: int
    currency: str

    @classmethod
    def from_domain(cls, money: Money) -> "MoneyOut":
        return cls(amount_cents=money.amount_cents, currency=money.currency)

    @classmethod
    def from_optional(cls, money: Money | None) -> "MoneyOut | None":
        return None if money is None else cls.from_domain(money)


class ReceiptSummaryOut(BaseModel):
    """``item_count`` counts the ITEM lines (not discounts, deposits or fees); ``savings``
    (>= 0) is what the receipt's discount lines took off."""

    id: int
    status: ReceiptStatus
    store_name: str
    purchased_on: date | None
    total: MoneyOut | None
    item_count: int
    created_at: datetime
    savings: MoneyOut

    @classmethod
    def from_domain(cls, summary: ReceiptSummary) -> "ReceiptSummaryOut":
        return cls(
            id=summary.id,
            status=summary.status,
            store_name=summary.store_name,
            purchased_on=summary.purchased_on,
            total=MoneyOut.from_optional(summary.total),
            item_count=summary.item_count,
            created_at=summary.created_at,
            savings=MoneyOut.from_domain(summary.savings),
        )


class ReceiptUploadOut(BaseModel):
    id: int
    status: ReceiptStatus
    stage: ProcessingStage

    @classmethod
    def from_domain(cls, receipt: Receipt) -> "ReceiptUploadOut":
        return cls(id=require_id(receipt.id), status=receipt.status, stage=receipt.stage)


class ReceiptPageOut(BaseModel):
    items: list[ReceiptSummaryOut]
    next_cursor: str | None  # pass back as ?cursor= for the next page; null on the last page

    @classmethod
    def from_domain(cls, page: Page[ReceiptSummary]) -> "ReceiptPageOut":
        return cls(
            items=[ReceiptSummaryOut.from_domain(summary) for summary in page.items],
            next_cursor=page.next_cursor,
        )


class LineItemOut(BaseModel):
    """``product_name`` and ``product_category`` are the linked product's, if any."""

    id: int
    position: int
    kind: LineKind
    raw_text: str
    description: str
    quantity: str  # the exact decimal, e.g. "0.452"
    unit_price: MoneyOut | None
    line_total: MoneyOut
    product_id: int | None
    category: Category | None
    applies_to_position: int | None
    match_source: MatchSource | None
    product_name: str | None
    product_category: Category | None

    @classmethod
    def from_domain(cls, item: LineItem, product: Product | None) -> "LineItemOut":
        return cls(
            id=require_id(item.id),
            position=item.position,
            kind=item.kind,
            raw_text=item.raw_text,
            description=item.description,
            quantity=str(item.quantity),
            unit_price=MoneyOut.from_optional(item.unit_price),
            line_total=MoneyOut.from_domain(item.line_total),
            product_id=item.product_id,
            category=item.category,
            applies_to_position=item.applies_to_position,
            match_source=item.match_source,
            product_name=None if product is None else product.canonical_name,
            product_category=None if product is None else product.category,
        )


class ArithmeticIssueOut(BaseModel):
    code: IssueCode
    message: str
    expected: MoneyOut | None
    actual: MoneyOut | None
    position: int | None

    @classmethod
    def from_domain(cls, issue: ArithmeticIssue) -> "ArithmeticIssueOut":
        return cls(
            code=issue.code,
            message=issue.message,
            expected=MoneyOut.from_optional(issue.expected),
            actual=MoneyOut.from_optional(issue.actual),
            position=issue.position,
        )


class ArithmeticReportOut(BaseModel):
    ok: bool
    issues: list[ArithmeticIssueOut]

    @classmethod
    def from_domain(cls, report: ArithmeticReport) -> "ArithmeticReportOut":
        return cls(
            ok=report.ok, issues=[ArithmeticIssueOut.from_domain(issue) for issue in report.issues]
        )


class ReceiptDetailOut(ReceiptSummaryOut):
    stage: ProcessingStage
    subtotal: MoneyOut | None
    tax: MoneyOut | None
    payment_last4: str | None
    has_image: bool
    items: list[LineItemOut]
    arithmetic: ArithmeticReportOut

    @classmethod
    def from_receipt(
        cls, receipt: Receipt, report: ArithmeticReport, products: Mapping[int, Product]
    ) -> "ReceiptDetailOut":
        """``products`` holds the products the lines link to, by id."""
        discounts = [
            item.line_total.amount_cents for item in receipt.items if item.kind is LineKind.DISCOUNT
        ]
        return cls(
            id=require_id(receipt.id),
            status=receipt.status,
            store_name=receipt.store_name,
            purchased_on=receipt.purchased_on,
            total=MoneyOut.from_optional(receipt.total),
            item_count=sum(1 for item in receipt.items if item.kind is LineKind.ITEM),
            created_at=receipt.created_at,
            savings=MoneyOut(amount_cents=-sum(discounts), currency=receipt.currency),
            stage=receipt.stage,
            subtotal=MoneyOut.from_optional(receipt.subtotal),
            tax=MoneyOut.from_optional(receipt.tax),
            payment_last4=receipt.payment_last4,
            has_image=receipt.image_path is not None,
            items=[
                LineItemOut.from_domain(
                    item, None if item.product_id is None else products.get(item.product_id)
                )
                for item in receipt.items
            ],
            arithmetic=ArithmeticReportOut.from_domain(report),
        )
