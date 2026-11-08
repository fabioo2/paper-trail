"""The tools themselves: plain functions over the ports and use cases (agent-core plan 2.3).

Specialists only read (``ocr_read``, ``check_arithmetic``, ``product_lookup``,
``product_similarity_search``); every write is a supervisor tool (``save_receipt``,
``open_review``, ``save_matches``, ``save_enrichment``). Domain errors such as NotFoundError,
and the embedding host's LlmUnavailableError, propagate unchanged.
"""

from dataclasses import dataclass

from paper_trail.application.enrichment import save_enrichment as record_enrichment
from paper_trail.application.matching import find_product
from paper_trail.application.payloads import IssuePayload
from paper_trail.application.receipts import RecordReceipt
from paper_trail.domain.enums import ProcessingStage, ReceiptStatus
from paper_trail.domain.models import require_id
from paper_trail.domain.rules.arithmetic import Tolerances, check_arithmetic
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.ports.embeddings import EmbeddingPort
from paper_trail.ports.images import ImageStore
from paper_trail.ports.ocr import OcrPort
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from paper_trail.tools.models import (
    CandidateOut,
    CheckArithmeticInput,
    CheckArithmeticOutput,
    LinkedLineOut,
    OcrReadInput,
    OcrReadOutput,
    OpenReviewInput,
    OpenReviewOutput,
    ProductLookupInput,
    ProductLookupOutput,
    ProductOut,
    SaveEnrichmentInput,
    SaveEnrichmentOutput,
    SaveMatchesInput,
    SaveMatchesOutput,
    SaveReceiptInput,
    SaveReceiptOutput,
    SimilaritySearchInput,
    SimilaritySearchOutput,
)


@dataclass(frozen=True, slots=True)
class ToolServices:
    """What the tools work with, wired by the composition root."""

    ocr: OcrPort
    image_store: ImageStore
    uow_factory: UnitOfWorkFactory
    record_receipt: RecordReceipt
    tolerances: Tolerances
    embeddings: EmbeddingPort


def ocr_read(services: ToolServices, request: OcrReadInput) -> OcrReadOutput:
    """OCR a stored image. Card numbers are masked here, before the text leaves the tool, so
    no unmasked text reaches graph state, checkpoints, prompts or traces (AC-A-04)."""
    result = services.ocr.read(services.image_store.open(request.image_path))
    masked = mask_pans(result.text)
    return OcrReadOutput(
        text=masked.text,
        mean_confidence=result.mean_confidence,
        rotation_degrees=result.rotation_degrees,
        masked_pans=masked.masked_count,
    )


def check_arithmetic_tool(
    services: ToolServices, request: CheckArithmeticInput
) -> CheckArithmeticOutput:
    """Does the receipt add up? The domain check, its issues verbatim."""
    draft = request.receipt.to_draft(today=None, ocr_text=None, image_path=None)
    report = check_arithmetic(draft, services.tolerances)
    return CheckArithmeticOutput(
        ok=report.ok, issues=[IssuePayload.of(issue) for issue in report.issues]
    )


def save_receipt(services: ToolServices, request: SaveReceiptInput) -> SaveReceiptOutput:
    """Record the draft as the receipt's contents (``RecordReceipt.replace``). An accepted
    draft stays PROCESSING/MATCHING: ``save_matches`` completes the receipt once its lines are
    linked, so a receipt with unlinked lines never looks finished."""
    result = services.record_receipt.replace(
        request.receipt_id, request.draft.to_draft(), force=request.force, matching=True
    )
    receipt = result.receipt
    return SaveReceiptOutput(
        receipt_id=require_id(receipt.id),
        status=receipt.status,
        stage=receipt.stage,
        item_count=len(receipt.items),
        masked_pans=result.masked_pans,
    )


def open_review(services: ToolServices, request: OpenReviewInput) -> OpenReviewOutput:
    """Park the receipt for a person: an OPEN review item tied to the graph thread.

    Idempotent per receipt and thread: the supervisor opens the review before its checkpoint
    is written, so a run resumed after a crash in between calls this again and gets the review
    it already opened (the receipt is set NEEDS_REVIEW again, as the crash may have marked it
    FAILED). An OPEN review of another thread is still a ConflictError.
    """
    with services.uow_factory() as uow:
        review = uow.reviews.find_open(request.receipt_id)
        if review is None or review.thread_id != request.thread_id:
            review = uow.reviews.open(
                request.receipt_id, request.reason, request.details, thread_id=request.thread_id
            )
        uow.receipts.update_status(
            request.receipt_id, ReceiptStatus.NEEDS_REVIEW, ProcessingStage.NEEDS_REVIEW
        )
        uow.commit()
    return OpenReviewOutput(review_id=require_id(review.id))


def product_lookup(services: ToolServices, request: ProductLookupInput) -> ProductLookupOutput:
    """The product an alias names at a store: correction memory first, then an exact alias
    (the store's, then a global one). The alias is normalized here."""
    with services.uow_factory(read_only=True) as uow:
        found = find_product(uow, normalize_alias(request.alias), request.store_key)
    if found is None:
        return ProductLookupOutput(product=None, source=None)
    product, source = found
    return ProductLookupOutput(product=ProductOut.of(product), source=source)


def product_similarity_search(
    services: ToolServices, request: SimilaritySearchInput
) -> SimilaritySearchOutput:
    """The ``k`` products whose vectors are nearest the text's embedding, nearest first."""
    (vector,) = services.embeddings.embed([request.text])
    with services.uow_factory(read_only=True) as uow:
        candidates = [
            CandidateOut(product=ProductOut.of(product), distance=hit.distance)
            for hit in uow.vectors.nearest(vector, k=request.k)
            if (product := uow.products.get(hit.product_id)) is not None
        ]
    return SimilaritySearchOutput(candidates=candidates)


def save_matches(services: ToolServices, request: SaveMatchesInput) -> SaveMatchesOutput:
    """Link the receipt's lines to their products, all in one unit of work (idempotent)."""
    recorded = services.record_receipt.record_matches(
        request.receipt_id,
        [decision.to_choice() for decision in request.decisions],
        services.embeddings,
    )
    return SaveMatchesOutput(
        receipt_id=recorded.receipt_id,
        linked=len(recorded.links),
        new_products=recorded.new_products,
        prices=recorded.prices,
        lines=[
            LinkedLineOut(
                position=link.position, product_id=link.product_id, source=link.match_source
            )
            for link in recorded.links
        ],
    )


def save_enrichment(services: ToolServices, request: SaveEnrichmentInput) -> SaveEnrichmentOutput:
    """Describe a product (name, brand, size, category, tags) and re-embed it; a person's
    category for the alias at the store wins over the model's (idempotent)."""
    saved = record_enrichment(
        services.uow_factory,
        services.embeddings,
        request.product_id,
        alias=request.alias,
        store_key=request.store_key,
        enrichment=request.enrichment,
    )
    return SaveEnrichmentOutput(
        product=ProductOut.of(saved.product), category_source=saved.category_source
    )
