"""Entities and drafts of the domain (PLAN 3.2). All frozen, with tuples for collections.

Unsaved entities have ``id=None``; repositories return them with the id assigned by storage.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from paper_trail.domain.enums import (
    AliasSource,
    Category,
    CodeScheme,
    CodeSource,
    CorrectionKind,
    FieldSource,
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
    """One printed line of a receipt. A DISCOUNT line's ``applies_to_position`` names the ITEM
    line it reduces."""

    position: int  # 0-based order on the receipt
    kind: LineKind
    raw_text: str  # as printed (card numbers masked)
    description: str  # human-readable normalized description
    quantity: Decimal  # > 0; fractional for weighed goods
    unit_price: Money | None
    line_total: Money  # negative only for DISCOUNT lines and a DEPOSIT returned
    product_id: int | None = None
    category: Category | None = None
    id: int | None = None
    match_source: MatchSource | None = None  # how product_id was found (agent core C)
    applies_to_position: int | None = None

    def __post_init__(self) -> None:
        if not self.quantity.is_finite() or self.quantity <= 0:
            msg = f"line {self.position + 1}: quantity must be positive, got {self.quantity}"
            raise InvalidQuantityError(msg)
        target = self.applies_to_position
        if target is None:
            return
        line = f"line {self.position + 1}: applies_to_position"
        if self.kind is not LineKind.DISCOUNT:
            msg = f"{line} is set on a {self.kind.value} line; only a discount applies to an item"
            raise DomainValidationError(msg)
        if target < 0:
            msg = f"{line} must name another line of the receipt"
            raise DomainValidationError(msg)
        if target == self.position:
            msg = f"{line} names line {target + 1}, its own line; it must name another line"
            raise DomainValidationError(msg)


def _check_discount_links(items: Sequence[LineItem]) -> None:
    """Every ``applies_to_position`` names an ITEM line of ``items``, no ITEM line twice, the
    discount is negative and no larger than the item's line total; DomainValidationError
    naming both lines (1-based, like every line message) otherwise."""
    by_position = {item.position: item for item in items}
    linked: dict[int, int] = {}
    for discount in items:
        target = discount.applies_to_position
        if target is None:
            continue
        line = f"line {discount.position + 1}: applies_to_position names line {target + 1}"
        item = by_position.get(target)
        if item is None:
            msg = f"{line}, which is not on the receipt"
            raise DomainValidationError(msg)
        if item.kind is not LineKind.ITEM:
            msg = f"{line}, a {item.kind.value} line, not an item"
            raise DomainValidationError(msg)
        if target in linked:
            msg = (
                f"{line}, which line {linked[target] + 1} already applies to;"
                " an item takes one discount"
            )
            raise DomainValidationError(msg)
        if not discount.line_total.is_negative():
            msg = f"{line}, but the discount must be negative"
            raise DomainValidationError(msg)
        if item.line_total.amount_cents + discount.line_total.amount_cents < 0:
            msg = f"{line}, but the discount is larger than that line's total"
            raise DomainValidationError(msg)
        linked[target] = discount.position


@dataclass(frozen=True, slots=True)
class ItemLink:
    """One receipt line linked to a product, and how the product was found."""

    position: int
    product_id: int
    match_source: MatchSource


@dataclass(frozen=True, slots=True)
class ProductLine:
    """A stored ITEM line linked to a product, with where and when its receipt was bought."""

    receipt_id: int
    store_key: str
    purchased_on: date | None
    line: LineItem


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
        _check_discount_links(self.items)


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
        _check_discount_links(self.items)


@dataclass(frozen=True, slots=True)
class ReceiptSummary:
    """A receipt as listed: no lines, just how many are ITEM lines (``item_count``) and what its
    discounts took off (``savings``, >= 0)."""

    id: int
    status: ReceiptStatus
    store_name: str
    purchased_on: date | None
    total: Money | None
    item_count: int
    created_at: datetime
    savings: Money


@dataclass(frozen=True, slots=True)
class ReceiptSpend:
    """A COMPLETED receipt as counted in a month's spending: its store, date, total and what
    its discounts took off (``savings``, >= 0)."""

    store_key: str
    store_name: str
    purchased_on: date
    total: Money | None
    savings: Money


@dataclass(frozen=True, slots=True)
class WeekSpend:
    """Spending from ``start`` to ``end`` (inclusive): one sum per currency, largest first."""

    start: date
    end: date
    spent: tuple[Money, ...]


@dataclass(frozen=True, slots=True)
class StoreSpend:
    store_name: str
    spent: Money


@dataclass(frozen=True, slots=True)
class MonthSummary:
    """A month's COMPLETED receipts by purchase date (``spent`` and ``saved`` one sum per
    currency, largest spend first), and what the app has waiting right now."""

    month: date  # its first day
    spent: tuple[Money, ...]
    saved: tuple[Money, ...]
    trips: int
    weeks: tuple[WeekSpend, ...]
    stores: tuple[StoreSpend, ...]
    open_reviews: int
    unconfirmed_products: int
    processing: int
    recent: tuple[ReceiptSummary, ...]


@dataclass(frozen=True, slots=True)
class ReceiptMatch:
    """A receipt found by a search, with the descriptions of its lines that matched."""

    receipt: ReceiptSummary
    lines: tuple[str, ...]


# The keys of ``Product.field_sources``; ``size`` covers ``size_value`` and ``size_unit``.
FIELD_SOURCE_KEYS = ("canonical_name", "brand", "size", "category", "tags")
# A product is confirmed when one of these named it: a person or a data source, never a model.
CONFIRMING_SOURCES = frozenset(
    {FieldSource.PERSON, FieldSource.PLU, FieldSource.OFF, FieldSource.UPC}
)
# The sources enrichment may write over (an empty field too): the printed text and a guess.
OPEN_SOURCES = frozenset({FieldSource.RECEIPT_TEXT, FieldSource.LLM})
PERSON_CODE_SOURCES = frozenset(
    {
        CodeSource.MANUAL,
        CodeSource.IMPORT,
        CodeSource.ADOPTED,
        CodeSource.CORRECTION,
        CodeSource.REVIEW,
    }
)


@dataclass(frozen=True, slots=True)
class Product:
    """``field_sources`` maps a filled field (``FIELD_SOURCE_KEYS``) to where it came from."""

    id: int | None
    canonical_name: str
    brand: str | None
    size_value: Decimal | None
    size_unit: Unit | None
    category: Category | None
    tags: tuple[str, ...]
    created_at: datetime
    field_sources: Mapping[str, FieldSource] = field(default_factory=dict)


def is_confirmed(product: Product) -> bool:
    """Whether a person or a data source named the product (its ``canonical_name`` source)."""
    return product.field_sources.get("canonical_name") in CONFIRMING_SOURCES


@dataclass(frozen=True, slots=True)
class LastPurchase:
    """A product's newest ITEM line: its receipt's date and store, and what was paid (the line's
    price observation, or its line total when the receipt has no date and so no observation)."""

    purchased_on: date | None
    store_name: str
    paid: Money


@dataclass(frozen=True, slots=True)
class ProductSummary:
    """A product as listed: how many ITEM lines name it and the newest of them."""

    product: Product
    times_bought: int
    last_purchase: LastPurchase


@dataclass(frozen=True, slots=True)
class ProductPurchase:
    """One ITEM line naming a product: its receipt's date and store, and the line's price
    observation (an undated receipt has none: its line's own prices, no discount)."""

    receipt_id: int
    purchased_on: date | None
    store_name: str
    quantity: Decimal
    unit_price: Money
    discount: Money
    paid: Money


@dataclass(frozen=True, slots=True)
class CodeIdentity:
    """What a code is stored under: ``store_key`` is ``""`` unless the scheme is STORE."""

    scheme: CodeScheme
    store_key: str
    code: str

    def __str__(self) -> str:
        """``store costco 1362394``, ``gtin 0060383064877``, ``plu 93101``."""
        return " ".join(part for part in (self.scheme, self.store_key, self.code) if part)


@dataclass(frozen=True, slots=True)
class ProductCode:
    """A code that names one product (``product_codes``, unique on its identity)."""

    id: int | None
    product_id: int
    scheme: CodeScheme
    store_key: str  # "" unless scheme is STORE
    code: str
    source: CodeSource
    created_at: datetime
    updated_at: datetime

    @property
    def identity(self) -> CodeIdentity:
        return CodeIdentity(self.scheme, self.store_key, self.code)


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
    """What one product cost on one receipt line: ``unit_price`` and ``line_total`` are the
    regular prices, ``discount`` (<= 0) the line's linked discount."""

    id: int | None
    product_id: int
    receipt_id: int
    line_item_id: int
    store_key: str
    observed_on: date
    quantity: Decimal
    unit_price: Money
    line_total: Money
    discount: Money

    def __post_init__(self) -> None:
        _check_currency(
            self.unit_price.currency, [self.line_total, self.discount], "a price observation"
        )
        if self.discount.amount_cents > 0:
            msg = f"a price observation's discount must be <= 0, got {self.discount.amount_text}"
            raise DomainValidationError(msg)
        if self.paid.is_negative():
            msg = (
                f"a price observation's discount {self.discount.amount_text} is larger than its"
                f" line total {self.line_total.amount_text}"
            )
            raise DomainValidationError(msg)

    @property
    def paid(self) -> Money:
        return self.line_total + self.discount

    @property
    def unit_discount(self) -> Money:
        return self.discount.divided_by(self.quantity)

    @property
    def paid_unit_price(self) -> Money:
        return self.paid.divided_by(self.quantity)

    @property
    def on_sale(self) -> bool:
        return self.discount.is_negative()


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
    payload: Mapping[str, JsonValue] | None = None  # what the job needs besides its receipt


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
