"""The receipt graph's state: JSON values only, so every checkpoint round-trips (plan 2.4).

Nodes return partial updates; each key is overwritten by the latest update. Text in the state
is always masked: OCR text is masked by the ``ocr_read`` tool or before a text input starts.
"""

from typing import Any, TypedDict


class ReceiptState(TypedDict, total=False):
    receipt_id: int
    source_name: str
    image_path: str | None  # of the stored image; None for a text input
    ocr_text: str | None  # masked
    ocr_confidence: float | None
    # How far the OCR turned the photo to read it (counter-clockwise); the vision attempt sees
    # the photo turned the same way. Absent in older checkpoints: 0.
    ocr_rotation: int
    masked_pans: int
    # The latest extraction attempt: ExtractionOutput as JSON, or None when it did not parse.
    extraction: dict[str, Any] | None
    parse_error: str | None
    valid: bool  # the latest extraction adds up
    issues: list[dict[str, Any]]  # its arithmetic issues (IssuePayload JSON)
    feedback: list[str]  # lines for the next attempt's <validation_feedback>
    text_attempts: int
    vision_attempts: int
    route: str  # the supervisor's latest target
    review_reason: str | None
    review_id: int | None
    draft: dict[str, Any] | None  # a person's answer to the review (DraftPayload JSON)
    approved: bool  # a person approved what is saved
    status: str | None  # the receipt's status once saved
    saved: bool
    # The matching agent's decisions, one per matched line: {position, source, product_id,
    # new_product_name}, the ``save_matches`` tool's input (slice C).
    matches: list[dict[str, Any]]
    # The lines ``save_matches`` linked to a product created for them, {position, product_id},
    # in line order: the products the enrichment agent describes (slice D).
    created: list[dict[str, Any]]
    # The enrichment agent's answers, one per product it described: {product_id, alias,
    # store_key, enrichment}, the ``save_enrichment`` tool's input (slice D).
    enrichments: list[dict[str, Any]]


def thread_id(receipt_id: int) -> str:
    """One graph thread per receipt; a review resumes it."""
    return f"receipt-{receipt_id}"
