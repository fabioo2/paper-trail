"""Input and output models of the tools: the JSON the gateway carries (ADR-0013)."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from paper_trail.application.matching import MatchChoice
from paper_trail.application.payloads import (
    CatalogEntryPayload,
    DraftPayload,
    ExtractedReceipt,
    FieldSourceKey,
    IssuePayload,
    ProductFieldsPayload,
    check_field_sources,
)
from paper_trail.domain.enums import (
    Category,
    CodeKind,
    CodeScheme,
    FieldSource,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
    ReviewReason,
    Unit,
)
from paper_trail.domain.errors import DomainValidationError
from paper_trail.domain.models import CodeIdentity, JsonValue, Product, require_id
from paper_trail.domain.rules.discounts import Refusal
from paper_trail.domain.rules.item_codes import ItemCode


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckArithmeticInput(_Model):
    receipt: ExtractedReceipt


class CheckArithmeticOutput(_Model):
    ok: bool
    issues: list[IssuePayload]


class SaveReceiptInput(_Model):
    receipt_id: StrictInt
    draft: DraftPayload
    force: bool = False  # a person approved the draft: COMPLETED even if it does not add up
    # False for a draft mapped from a Claude reading: its discount links are kept as they are
    # and no other is added (an order discount stays at receipt level).
    attribute_discounts: bool = True


class ReceiptLevelDiscountOut(_Model):
    """A discount kept at receipt level, and why."""

    position: int
    raw_text: str
    reason: Refusal


class SaveReceiptOutput(_Model):
    receipt_id: int
    status: ReceiptStatus
    stage: ProcessingStage
    item_count: int
    masked_pans: int
    discounts_linked: int
    discounts_receipt_level: list[ReceiptLevelDiscountOut]


class OpenReviewInput(_Model):
    receipt_id: StrictInt
    reason: ReviewReason
    details: dict[str, JsonValue]
    thread_id: StrictStr  # the graph thread a resolution resumes


class OpenReviewOutput(_Model):
    review_id: int


class ProductOut(_Model):
    id: int
    canonical_name: str
    brand: str | None
    size_value: str | None  # a decimal string, as everywhere in JSON
    size_unit: Unit | None
    category: Category | None
    tags: list[str]
    field_sources: dict[str, FieldSource]

    @classmethod
    def of(cls, product: Product) -> Self:
        return cls(
            id=require_id(product.id),
            canonical_name=product.canonical_name,
            brand=product.brand,
            size_value=None if product.size_value is None else str(product.size_value),
            size_unit=product.size_unit,
            category=product.category,
            tags=list(product.tags),
            field_sources=dict(product.field_sources),
        )


class ProductLookupInput(_Model):
    alias: StrictStr  # normalized by the tool
    store_key: StrictStr | None = None  # None: global aliases only


class ProductLookupOutput(_Model):
    product: ProductOut | None
    source: Literal[MatchSource.MEMORY, MatchSource.ALIAS] | None  # memory, or an exact alias


class ItemCodeOut(_Model):
    """A line's leading code as parsed at the store (``ItemCode``); ``identity`` is what
    ``product_codes`` stores it under (None for kind ``none``)."""

    kind: CodeKind
    printed: str
    keys: list[str]
    scheme: CodeScheme | None
    identity: str | None

    @classmethod
    def of(cls, code: ItemCode) -> Self:
        return cls(
            kind=code.kind,
            printed=code.printed,
            keys=list(code.keys),
            scheme=code.scheme,
            identity=code.identity,
        )


class CatalogLookupInput(_Model):
    raw_text: StrictStr  # the line's raw text; parsed by the tool when ``code`` is None
    store_key: StrictStr
    code: StrictStr | None = None  # the code a reading gave the line, read instead of the text


class CatalogLookupOutput(_Model):
    code: ItemCodeOut | None  # None: the line has no leading code
    product: ProductOut | None  # ours, when the code is already one of our products
    entry: CatalogEntryPayload | None  # the source that hit (the store catalog's, for ours)


class CodeRefIn(_Model):
    """A code identity: ``store_key`` is "" unless the scheme is ``store``."""

    scheme: CodeScheme
    store_key: StrictStr
    code: StrictStr = Field(min_length=1)

    def to_identity(self) -> CodeIdentity:
        return CodeIdentity(self.scheme, self.store_key, self.code)


class MatchDecisionIn(_Model):
    """One line's product: an existing one, or a new one (source ``new``) named here.

    ``code``: the line's code identity when it was matched by its code; ``name_source``: where
    a new product's name came from (``receipt_text`` when absent); ``category_source``: where
    the line's own category came from (``person`` when absent); ``catalog``: the source entry
    it was named from, kept for the enrichment agent (not written)."""

    position: StrictInt = Field(ge=0)
    source: MatchSource
    product_id: StrictInt | None = None
    new_product_name: StrictStr | None = Field(default=None, min_length=1)
    code: CodeRefIn | None = None
    name_source: FieldSource | None = None
    category_source: FieldSource | None = None
    printed_code: StrictStr | None = None  # the code a reading gave the line, if ``code`` is it
    catalog: CatalogEntryPayload | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        try:
            self.to_choice()
        except DomainValidationError as error:
            raise ValueError(str(error)) from None
        return self

    def to_choice(self) -> MatchChoice:
        """The decision as the use case takes it (which checks it is consistent)."""
        return MatchChoice(
            position=self.position,
            source=self.source,
            product_id=self.product_id,
            new_product_name=self.new_product_name,
            code=None if self.code is None else self.code.to_identity(),
            name_source=self.name_source,
            category_source=self.category_source,
            printed_code=self.printed_code,
        )


class SaveMatchesInput(_Model):
    receipt_id: StrictInt
    decisions: list[MatchDecisionIn]


class LinkedLineOut(_Model):
    position: int
    product_id: int
    source: MatchSource


class UnpricedLineOut(_Model):
    """A linked item line with a negative total, which gets no price observation."""

    position: int
    raw_text: str


class SaveMatchesOutput(_Model):
    receipt_id: int
    linked: int
    new_products: int
    prices: int
    lines: list[LinkedLineOut]
    unpriced: list[UnpricedLineOut]


class DescribeFromCatalogInput(_Model):
    """``fields`` and ``sources`` pair up (``check_field_sources``); ``alias`` (the raw text of
    the line the product came from) and ``store_key`` key a person's category correction."""

    product_id: StrictInt
    fields: ProductFieldsPayload
    sources: dict[FieldSourceKey, FieldSource]
    alias: StrictStr
    store_key: StrictStr

    @model_validator(mode="after")
    def _paired(self) -> Self:
        try:
            check_field_sources(self.fields, self.sources)
        except DomainValidationError as error:
            raise ValueError(str(error)) from None
        return self


class DescribeFromCatalogOutput(_Model):
    product: ProductOut  # as stored
