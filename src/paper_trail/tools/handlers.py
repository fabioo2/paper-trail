"""The tools themselves: plain functions over the ports and use cases (agent-core plan 2.3).

Specialists only read (``check_arithmetic``, ``product_lookup``, ``catalog_lookup``); every
write is a supervisor tool (``save_receipt``, ``open_review``, ``save_matches``,
``describe_from_catalog``). Domain errors such as NotFoundError propagate unchanged.
"""

from dataclasses import dataclass

from paper_trail.application.catalog import describe_product
from paper_trail.application.matching import find_product
from paper_trail.application.payloads import CatalogEntryPayload, IssuePayload
from paper_trail.application.receipts import RecordReceipt
from paper_trail.domain.enums import ProcessingStage, ReceiptStatus
from paper_trail.domain.models import require_id
from paper_trail.domain.rules.arithmetic import Tolerances, check_arithmetic
from paper_trail.domain.rules.item_codes import parse_code_field, parse_item_code
from paper_trail.ports.catalog import ProductCatalog
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from paper_trail.tools.models import (
    CatalogLookupInput,
    CatalogLookupOutput,
    CheckArithmeticInput,
    CheckArithmeticOutput,
    DescribeFromCatalogInput,
    DescribeFromCatalogOutput,
    ItemCodeOut,
    LinkedLineOut,
    OpenReviewInput,
    OpenReviewOutput,
    ProductLookupInput,
    ProductLookupOutput,
    ProductOut,
    ReceiptLevelDiscountOut,
    SaveMatchesInput,
    SaveMatchesOutput,
    SaveReceiptInput,
    SaveReceiptOutput,
    UnpricedLineOut,
)


@dataclass(frozen=True, slots=True)
class ToolServices:
    """What the tools work with, wired by the composition root."""

    uow_factory: UnitOfWorkFactory
    record_receipt: RecordReceipt
    tolerances: Tolerances
    catalog: ProductCatalog  # the store catalog, then the reference sources


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
        request.receipt_id,
        request.draft.to_draft(),
        force=request.force,
        matching=True,
        attribute=request.attribute_discounts,
    )
    receipt = result.receipt
    raw_texts = {item.position: item.raw_text for item in receipt.items}
    return SaveReceiptOutput(
        receipt_id=require_id(receipt.id),
        status=receipt.status,
        stage=receipt.stage,
        item_count=len(receipt.items),
        masked_pans=result.masked_pans,
        discounts_linked=len(result.discounts.links),
        discounts_receipt_level=[
            ReceiptLevelDiscountOut(
                position=discount.discount_position,
                raw_text=raw_texts[discount.discount_position],
                reason=discount.reason,
            )
            for discount in result.discounts.receipt_level
        ],
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
    """The product a line's text names at a store: a person's correction first, then an exact
    alias at that store (a global one only when no store is given). The text may be raw: both
    of its alias keys are tried (``find_product``)."""
    with services.uow_factory(read_only=True) as uow:
        found = find_product(uow, request.alias, request.store_key)
    if found is None:
        return ProductLookupOutput(product=None, source=None)
    product, source = found
    return ProductLookupOutput(product=ProductOut.of(product), source=source)


def save_matches(services: ToolServices, request: SaveMatchesInput) -> SaveMatchesOutput:
    """Link the receipt's lines to their products, all in one unit of work (idempotent)."""
    recorded = services.record_receipt.record_matches(
        request.receipt_id, [decision.to_choice() for decision in request.decisions]
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
        unpriced=[
            UnpricedLineOut(position=line.position, raw_text=line.raw_text)
            for line in recorded.unpriced
        ],
    )


def catalog_lookup(services: ToolServices, request: CatalogLookupInput) -> CatalogLookupOutput:
    """What the line's code names at the store: the code as parsed (``code`` when given, by
    ``parse_code_field``, else the raw text's leading code), our product when the code is ours
    (with its field sources), and the entry of the first catalog that knows it. A line without
    a code, or whose code is kind ``none``, asks no catalog. Reads only."""
    code = (
        parse_item_code(request.raw_text, request.store_key)
        if request.code is None
        else parse_code_field(request.code, request.store_key)
    )
    if code is None:
        return CatalogLookupOutput(code=None, product=None, entry=None)
    found = None if code.scheme is None else services.catalog.lookup(code, request.store_key)
    product = None
    if found is not None and found.product_id is not None:
        with services.uow_factory(read_only=True) as uow:
            product = uow.products.get(found.product_id)
    return CatalogLookupOutput(
        code=ItemCodeOut.of(code),
        product=None if product is None else ProductOut.of(product),
        entry=None if found is None else CatalogEntryPayload.of(found),
    )


def describe_from_catalog(
    services: ToolServices, request: DescribeFromCatalogInput
) -> DescribeFromCatalogOutput:
    """Write the fields a source gave a product (``describe_product``); never embeds."""
    product = describe_product(
        services.uow_factory,
        request.product_id,
        fields=request.fields,
        sources=request.sources,
        alias=request.alias,
        store_key=request.store_key,
    )
    return DescribeFromCatalogOutput(product=ProductOut.of(product))
