"""Entities and drafts of the domain (PLAN 3.2). All frozen, with tuples for collections.

Unsaved entities have ``id=None``; repositories return them with the id assigned by storage.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from paper_trail.domain.enums import (
    AliasSource,
    Category,
    CorrectionKind,
    JobKind,
    JobStatus,
    LineKind,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
    ReviewReason,
    ReviewStatus,
    Unit,
)
from paper_trail.domain.errors import (
    CurrencyMismatchError,
    DomainValidationError,
    InvalidQuantityError,
)
from paper_trail.domain.money import Money

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None

_LAST4 = re.compile(r"[0-9]{4}")


def require_id(entity_id: int | None) -> int:
    """The id of an entity that must be stored; storage always assigns one."""
    if entity_id is None:
        msg = "expected a stored entity, but its id is None"
        raise ValueError(msg)
    return entity_id


def _check_payment_last4(value: str | None) -> None:
    if value is not None and not _LAST4.fullmatch(value):
        msg = f"payment_last4 must be four digits, got {value!r}"
        raise DomainValidationError(msg)


def _check_currency(currency: str, amounts: Iterable[Money | None], what: str) -> None:
    for amount in amounts:
        if amount is not None and amount.currency != currency:
            msg = f"{what} holds {amount.currency} amounts but is in {currency}"
            raise CurrencyMismatchError(msg)


@dataclass(frozen=True, slots=True)
class LineItem:
    """One printed line of a receipt."""

    position: int  # 0-based order on the receipt
    kind: LineKind
    raw_text: str  # as printed (card numbers masked)
    description: str  # human-readable normalized description
    quantity: Decimal  # > 0; fractional for weighed goods
    unit_price: Money | None
    line_total: Money  # negative only for DISCOUNT lines
    product_id: int | None = None
    category: Category | None = None
    id: int | None = None
    match_source: MatchSource | None = None  # how product_id was found (agent core C)

    def __post_init__(self) -> None:
        if not self.quantity.is_finite() or self.quantity <= 0:
            msg = f"line {self.position + 1}: quantity must be positive, got {self.quantity}"
            raise InvalidQuantityError(msg)


@dataclass(frozen=True, slots=True)
class ItemLink:
    """One receipt line linked to a product, and how the product was found."""

    position: int
    product_id: int
    match_source: MatchSource


@dataclass(frozen=True, slots=True)
class ReceiptDraft:
    """Input to RecordReceipt: produced by the CLI import (Stage 1) and the graph (Stage 3).

    A draft may be inconsistent, even mix currencies: the arithmetic check reports what is
    wrong with it.
    """

    store_name: str
    purchased_on: date | None
    currency: str
    subtotal: Money | None
    tax: Money | None
    total: Money
    items: tuple[LineItem, ...]
    payment_last4: str | None
    ocr_text: str | None
    image_path: str | None  # relative to DATA_DIR/images
    # position -> product name; only the import knows the products of its lines.
    product_names: Mapping[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_payment_last4(self.payment_last4)


@dataclass(frozen=True, slots=True)
class Receipt:
    """A stored receipt. Every amount on it is in the receipt's currency."""

    id: int | None
    status: ReceiptStatus
    stage: ProcessingStage
    store_name: str
    store_key: str  # normalize_store_name(store_name)
    purchased_on: date | None
    currency: str
    subtotal: Money | None
    tax: Money | None
    total: Money | None  # None while still processing (Stage 2+)
    payment_last4: str | None
    ocr_text: str | None
    image_path: str | None
    items: tuple[LineItem, ...]
    created_at: datetime
    updated_at: datetime
    image_sha256: str | None = None  # of the stored image; one receipt per image

    def __post_init__(self) -> None:
        _check_payment_last4(self.payment_last4)
        amounts = [self.subtotal, self.tax, self.total]
        amounts += [money for item in self.items for money in (item.unit_price, item.line_total)]
        _check_currency(self.currency, amounts, "a receipt")


@dataclass(frozen=True, slots=True)
class ReceiptSummary:
    """A receipt as listed: no lines, just their count."""

    id: int
    status: ReceiptStatus
    store_name: str
    purchased_on: date | None
    total: Money | None
    item_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Product:
    id: int | None
    canonical_name: str
    brand: str | None
    size_value: Decimal | None
    size_unit: Unit | None
    category: Category | None
    tags: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProductAlias:
    """A text that names a product: ``normalize_alias`` output, global or for one store."""

    id: int | None
    product_id: int
    alias: str
    store_key: str | None  # None applies to every store
    source: AliasSource
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PriceObservation:
    """What one product cost on one receipt line."""

    id: int | None
    product_id: int
    receipt_id: int
    line_item_id: int
    store_key: str
    observed_on: date
    quantity: Decimal
    unit_price: Money
    line_total: Money

    def __post_init__(self) -> None:
        _check_currency(self.unit_price.currency, [self.line_total], "a price observation")


@dataclass(frozen=True, slots=True)
class Job:
    id: int | None
    kind: JobKind
    receipt_id: int
    status: JobStatus
    attempts: int
    max_attempts: int
    available_at: datetime
    locked_by: str | None
    lease_expires_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewItem:
    id: int | None
    receipt_id: int
    reason: ReviewReason
    details: Mapping[str, JsonValue]
    status: ReviewStatus
    thread_id: str | None
    created_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class Correction:
    """A user's fix, remembered so that the same mistake is not made twice."""

    id: int | None
    kind: CorrectionKind
    subject: str  # e.g. the normalized alias text, or "receipt:12.total"
    old_value: str | None
    new_value: str
    store_key: str | None
    receipt_id: int | None
    line_item_id: int | None
    created_at: datetime
