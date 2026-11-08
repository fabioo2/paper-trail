"""Input and output models of the tools: the JSON the gateway carries (ADR-0013)."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from paper_trail.application.enrichment import CategorySource
from paper_trail.application.matching import MatchChoice
from paper_trail.application.payloads import (
    DraftPayload,
    ExtractedReceipt,
    IssuePayload,
    ProductEnrichment,
)
from paper_trail.domain.enums import (
    Category,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
    ReviewReason,
    Unit,
)
from paper_trail.domain.errors import DomainValidationError
from paper_trail.domain.models import JsonValue, Product, require_id


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OcrReadInput(_Model):
    image_path: StrictStr  # relative to the image store, as stored on the receipt


class OcrReadOutput(_Model):
    text: str  # card numbers masked
    mean_confidence: float | None
    rotation_degrees: int  # how far the OCR turned the image to read it
    masked_pans: int


class CheckArithmeticInput(_Model):
    receipt: ExtractedReceipt


class CheckArithmeticOutput(_Model):
    ok: bool
    issues: list[IssuePayload]


class SaveReceiptInput(_Model):
    receipt_id: StrictInt
    draft: DraftPayload
    force: bool = False  # a person approved the draft: COMPLETED even if it does not add up


class SaveReceiptOutput(_Model):
    receipt_id: int
    status: ReceiptStatus
    stage: ProcessingStage
    item_count: int
    masked_pans: int


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
        )


class ProductLookupInput(_Model):
    alias: StrictStr  # normalized by the tool
    store_key: StrictStr | None = None  # None: global aliases only


class ProductLookupOutput(_Model):
    product: ProductOut | None
    source: Literal[MatchSource.MEMORY, MatchSource.ALIAS] | None  # memory, or an exact alias


class SimilaritySearchInput(_Model):
    text: StrictStr  # embedded as given
    k: StrictInt = Field(ge=1, le=10)


class CandidateOut(_Model):
    product: ProductOut
    distance: float  # cosine distance, 0 = the same direction


class SimilaritySearchOutput(_Model):
    candidates: list[CandidateOut]  # nearest first


class MatchDecisionIn(_Model):
    """One line's product: an existing one, or a new one (source ``new``) named here."""

    position: StrictInt = Field(ge=0)
    source: MatchSource
    product_id: StrictInt | None = None
    new_product_name: StrictStr | None = Field(default=None, min_length=1)

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
        )


class SaveMatchesInput(_Model):
    receipt_id: StrictInt
    decisions: list[MatchDecisionIn]


class LinkedLineOut(_Model):
    position: int
    product_id: int
    source: MatchSource


class SaveMatchesOutput(_Model):
    receipt_id: int
    linked: int
    new_products: int
    prices: int
    lines: list[LinkedLineOut]


class SaveEnrichmentInput(_Model):
    product_id: StrictInt
    alias: StrictStr  # the raw text of the line the product came from; normalized by the tool
    store_key: StrictStr | None  # where it was bought; with the alias, keys category memory
    enrichment: ProductEnrichment  # the model's answer without its confidence


class SaveEnrichmentOutput(_Model):
    product: ProductOut  # as stored
    category_source: CategorySource  # "memory" when a person's correction chose the category
