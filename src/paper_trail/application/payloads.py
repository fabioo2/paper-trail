"""JSON shapes of receipts that cross the tool boundary (PLAN 7, agent-core plan 2.3).

The agents and the tools are independent packages (import-linter contract 4): they exchange
JSON through the ``ToolGateway``, in process today and over MCP later. Both sides validate that
JSON with the models defined once here, below both:

- ``ExtractedReceipt``: a receipt as read from the paper (the extraction model's answer minus
  its confidence). Amounts are integer cents in one currency; the date is the text the model
  read, parsed in code (``parse_receipt_date``), because its format cannot be trusted.
- ``DraftPayload``: a ``ReceiptDraft`` without loss (each amount keeps its own currency), for
  saving a draft that came from the model or from a person.
- ``CatalogEntryPayload``: a catalog's entry for a code (the ``catalog_lookup`` tool's
  ``entry``), and ``ProductFieldsPayload``: the fields ``describe_from_catalog`` writes.
"""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    model_validator,
)

from paper_trail.domain.enums import Category, FieldSource, LineKind, Unit
from paper_trail.domain.errors import DomainValidationError
from paper_trail.domain.models import LineItem, ReceiptDraft
from paper_trail.domain.money import Money
from paper_trail.domain.rules.arithmetic import ArithmeticIssue, IssueCode
from paper_trail.domain.rules.dates import parse_receipt_date
from paper_trail.ports.catalog import CatalogProduct, CatalogSource


def _decimal_string(value: object) -> object:
    """Quantities are decimal strings such as "1.36" (or Decimals, from Python); a JSON number
    could already be inexact."""
    if not isinstance(value, str | Decimal):
        msg = 'quantity must be a decimal string, e.g. "1.36"'
        raise ValueError(msg)
    return value


def _positive_decimal_text(value: str) -> str:
    try:
        number = Decimal(value)
    except InvalidOperation:
        number = Decimal("NaN")
    if not number.is_finite() or number <= 0:
        msg = f'must be a positive decimal string, e.g. "1.36", got {value!r}'
        raise ValueError(msg)
    return value


def _not_blank(value: str) -> str:
    if not value.strip():
        msg = "must not be blank"
        raise ValueError(msg)
    return value


def _lowercase(value: str) -> str:
    if value != value.lower():
        msg = f"must be lowercase, got {value!r}"
        raise ValueError(msg)
    return value


Quantity = Annotated[Decimal, BeforeValidator(_decimal_string), Field(gt=0)]
# The model's quantity or size: kept as the decimal string it wrote (a JSON number is refused).
QuantityText = Annotated[StrictStr, AfterValidator(_positive_decimal_text)]
NonBlank = Annotated[StrictStr, AfterValidator(_not_blank)]
Tag = Annotated[StrictStr, Field(min_length=1), AfterValidator(_lowercase)]
MAX_TAGS = 8
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Last4 = Annotated[str, Field(pattern=r"^[0-9]{4}$")]


class ExtractedLine(BaseModel):
    """One printed line as read. Only DISCOUNT lines, and a DEPOSIT returned, are negative."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_text: str
    description: str
    quantity: QuantityText
    unit_price_cents: StrictInt | None
    line_total_cents: StrictInt
    kind: LineKind


class ExtractedReceipt(BaseModel):
    """A receipt as read from the paper (the ``check_arithmetic`` tool's input)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    store_name: str
    purchased_on: str | None  # as read; parsed in code
    currency: CurrencyCode
    subtotal_cents: StrictInt | None
    tax_cents: StrictInt | None
    total_cents: StrictInt
    payment_last4: Last4 | None
    items: list[ExtractedLine]

    def to_draft(
        self, *, today: date | None, ocr_text: str | None, image_path: str | None
    ) -> ReceiptDraft:
        """The draft to check or record; every amount in the receipt's currency.

        The date is parsed against ``today``: unreadable, impossible or future dates are
        unknown. ``today=None`` leaves the date unknown, for checks that do not need it.
        """
        currency = self.currency

        def money(cents: int | None) -> Money | None:
            return None if cents is None else Money(cents, currency)

        return ReceiptDraft(
            store_name=self.store_name,
            purchased_on=None
            if today is None or self.purchased_on is None
            else parse_receipt_date(self.purchased_on, today),
            currency=currency,
            subtotal=money(self.subtotal_cents),
            tax=money(self.tax_cents),
            total=Money(self.total_cents, currency),
            items=tuple(
                LineItem(
                    position=position,
                    kind=line.kind,
                    raw_text=line.raw_text,
                    description=line.description,
                    quantity=Decimal(line.quantity),
                    unit_price=money(line.unit_price_cents),
                    line_total=Money(line.line_total_cents, currency),
                )
                for position, line in enumerate(self.items)
            ),
            payment_last4=self.payment_last4,
            ocr_text=ocr_text,
            image_path=image_path,
        )


class MoneyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    amount_cents: StrictInt
    currency: CurrencyCode

    @classmethod
    def of(cls, money: Money) -> Self:
        return cls(amount_cents=money.amount_cents, currency=money.currency)

    def to_money(self) -> Money:
        return Money(self.amount_cents, self.currency)


def _payload(money: Money | None) -> MoneyPayload | None:
    return None if money is None else MoneyPayload.of(money)


def _money(payload: MoneyPayload | None) -> Money | None:
    return None if payload is None else payload.to_money()


class IssuePayload(BaseModel):
    """One arithmetic issue, as review items and the ``check_arithmetic`` tool report it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: IssueCode
    message: str
    expected: MoneyPayload | None
    actual: MoneyPayload | None
    position: int | None

    @classmethod
    def of(cls, issue: ArithmeticIssue) -> Self:
        return cls(
            code=issue.code,
            message=issue.message,
            expected=_payload(issue.expected),
            actual=_payload(issue.actual),
            position=issue.position,
        )

    def to_issue(self) -> ArithmeticIssue:
        return ArithmeticIssue(
            self.code, self.message, _money(self.expected), _money(self.actual), self.position
        )


class DraftLinePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position: StrictInt
    kind: LineKind
    raw_text: str
    description: str
    quantity: Quantity
    unit_price: MoneyPayload | None
    line_total: MoneyPayload
    category: Category | None
    applies_to_position: StrictInt | None = None
    unit: Unit | None = None

    @classmethod
    def from_line(cls, line: LineItem) -> Self:
        return cls(
            position=line.position,
            kind=line.kind,
            raw_text=line.raw_text,
            description=line.description,
            quantity=line.quantity,
            unit_price=_payload(line.unit_price),
            line_total=MoneyPayload.of(line.line_total),
            category=line.category,
            applies_to_position=line.applies_to_position,
            unit=line.unit,
        )

    def to_line(self) -> LineItem:
        return LineItem(
            position=self.position,
            kind=self.kind,
            raw_text=self.raw_text,
            description=self.description,
            quantity=self.quantity,
            unit_price=_money(self.unit_price),
            line_total=self.line_total.to_money(),
            category=self.category,
            applies_to_position=self.applies_to_position,
            unit=self.unit,
        )


class DraftPayload(BaseModel):
    """A ``ReceiptDraft`` as JSON, both ways without loss."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    store_name: str
    purchased_on: date | None
    currency: CurrencyCode
    subtotal: MoneyPayload | None
    tax: MoneyPayload | None
    total: MoneyPayload
    items: list[DraftLinePayload]
    payment_last4: Last4 | None
    ocr_text: str | None
    image_path: str | None
    product_names: dict[int, str]

    @classmethod
    def from_draft(cls, draft: ReceiptDraft) -> Self:
        return cls(
            store_name=draft.store_name,
            purchased_on=draft.purchased_on,
            currency=draft.currency,
            subtotal=_payload(draft.subtotal),
            tax=_payload(draft.tax),
            total=MoneyPayload.of(draft.total),
            items=[DraftLinePayload.from_line(line) for line in draft.items],
            payment_last4=draft.payment_last4,
            ocr_text=draft.ocr_text,
            image_path=draft.image_path,
            product_names=dict(draft.product_names),
        )

    def to_draft(self) -> ReceiptDraft:
        return ReceiptDraft(
            store_name=self.store_name,
            purchased_on=self.purchased_on,
            currency=self.currency,
            subtotal=_money(self.subtotal),
            tax=_money(self.tax),
            total=self.total.to_money(),
            items=tuple(line.to_line() for line in self.items),
            payment_last4=self.payment_last4,
            ocr_text=self.ocr_text,
            image_path=self.image_path,
            product_names=dict(self.product_names),
        )


class ReviewAnswerPayload(BaseModel):
    """A person's answer to a review, queued for the worker (a ``resolve_review`` job's
    payload): the corrected draft, masked, or None to accept the extraction the graph holds;
    ``force`` with ``reason`` keeps a draft that does not add up."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: StrictInt
    draft: DraftPayload | None
    force: bool
    reason: str | None


# The keys of ``Product.field_sources`` (``FIELD_SOURCE_KEYS``): ``size`` covers both size fields.
type FieldSourceKey = Literal["canonical_name", "brand", "size", "category", "tags"]


class CatalogEntryPayload(BaseModel):
    """A ``CatalogProduct`` as JSON (without ``product_id``: the tool returns our product
    itself). ``size_value`` is a decimal string."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: CatalogSource
    key: str
    name: str | None
    brand: str | None
    size_value: str | None
    size_unit: Unit | None
    quantity_text: str | None
    categories: list[str]
    category: Category | None
    tags: list[str]

    @classmethod
    def of(cls, entry: CatalogProduct) -> Self:
        return cls(
            source=entry.source,
            key=entry.key,
            name=entry.name,
            brand=entry.brand,
            size_value=None if entry.size_value is None else str(entry.size_value),
            size_unit=entry.size_unit,
            quantity_text=entry.quantity_text,
            categories=list(entry.categories),
            category=entry.category,
            tags=list(entry.tags),
        )


class ProductFieldsPayload(BaseModel):
    """The descriptive fields ``describe_from_catalog`` writes; None leaves a field as it is.
    ``size_value`` (a positive decimal string) and ``size_unit`` come together or not at all."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonBlank | None = None
    brand: NonBlank | None = None
    size_value: QuantityText | None = None
    size_unit: Unit | None = None
    category: Category | None = None
    tags: list[Tag] | None = Field(default=None, max_length=MAX_TAGS)

    @model_validator(mode="after")
    def _size_has_a_unit(self) -> Self:
        if (self.size_value is None) != (self.size_unit is None):
            msg = "size_value and size_unit are given together or not at all"
            raise ValueError(msg)
        return self


# The provenance key of each field ``describe_from_catalog`` takes.
_SOURCE_KEYS: dict[str, FieldSourceKey] = {
    "name": "canonical_name",
    "brand": "brand",
    "size_value": "size",
    "size_unit": "size",
    "category": "category",
    "tags": "tags",
}
_ENTRY_SOURCES = {"plu": FieldSource.PLU}
DATA_SOURCES = frozenset({FieldSource.PLU, FieldSource.OFF, FieldSource.UPC, FieldSource.LLM})


def entry_product_name(name: str, brand: str | None) -> str:
    """A new product's name from a source entry: ``"<brand> <name>"`` unless the name already
    contains the brand (ignoring case)."""
    if brand is None or brand.casefold() in name.casefold():
        return name
    return f"{brand} {name}"


def catalog_fields(
    entry: CatalogEntryPayload,
) -> tuple[ProductFieldsPayload, dict[FieldSourceKey, FieldSource]]:
    """The fields a reference entry supplies and their source: a ``plu`` entry gives its name,
    category and (when it has any) tags, each sourced ``plu``. A ``store_catalog`` entry is one
    of our products, so it gives nothing."""
    source = _ENTRY_SOURCES.get(entry.source)
    if source is None:
        return ProductFieldsPayload(), {}
    sized = entry.size_value is not None and entry.size_unit is not None
    fields = ProductFieldsPayload(
        name=entry.name,
        brand=entry.brand,
        size_value=entry.size_value if sized else None,
        size_unit=entry.size_unit if sized else None,
        category=entry.category,
        tags=list(entry.tags) or None,
    )
    return fields, dict.fromkeys(_given_keys(fields), source)


def check_field_sources(
    fields: ProductFieldsPayload, sources: Mapping[FieldSourceKey, FieldSource]
) -> None:
    """DomainValidationError unless every given field has exactly its source key and every
    source key has its field (``name`` -> ``canonical_name``, ``size_value``/``size_unit`` ->
    ``size``, the others by their own name), and every source is a data source (``plu``,
    ``off``, ``upc``, ``llm``): a person's values and the printed text are never written this
    way."""
    given = _given_keys(fields)
    problems = [
        f"field {field} has no source {_SOURCE_KEYS[field]}"
        for field in _given_fields(fields)
        if _SOURCE_KEYS[field] not in sources
    ]
    problems += [f"source {key} has no field" for key in sources if key not in given]
    problems += [
        f"source {key} is {source.value}, not a data source"
        for key, source in sources.items()
        if source not in DATA_SOURCES
    ]
    if problems:
        raise DomainValidationError("; ".join(problems))


def _given_fields(fields: ProductFieldsPayload) -> list[str]:
    return [name for name in _SOURCE_KEYS if getattr(fields, name) is not None]


def _given_keys(fields: ProductFieldsPayload) -> list[FieldSourceKey]:
    return list(dict.fromkeys(_SOURCE_KEYS[name] for name in _given_fields(fields)))
