"""The receipt graph's state: JSON values only, so every checkpoint round-trips (plan 2.4).

Nodes return partial updates; each key is overwritten by the latest update. Text in the state
is always masked: Claude's answer is masked by ``extract`` before it enters the state.
"""

from typing import Any, TypedDict

# The shape of the state below. A thread saved by an older version (2: OCR and the local
# models; none: before product codes) cannot be resumed (claude-extraction plan V7).
STATE_VERSION = 3


class ReceiptState(TypedDict, total=False):
    state_version: int  # STATE_VERSION; absent in threads saved before product codes
    receipt_id: int
    source_name: str
    image_path: str  # of the stored photo
    masked_pans: int  # card numbers masked in the latest answer
    # The latest read: Claude's ReceiptAnswer as JSON (masked), or None when it did not parse.
    extraction: dict[str, Any] | None
    parse_error: str | None
    valid: bool  # the latest extraction adds up
    issues: list[dict[str, Any]]  # its arithmetic issues (IssuePayload JSON)
    feedback: list[str]  # lines for the next attempt's <validation_feedback>
    attempts: int  # Claude reads made: attempt 1 on the extract model, 2 on the retry model
    route: str  # the supervisor's latest target
    review_reason: str | None
    review_id: int | None
    draft: dict[str, Any] | None  # a person's answer to the review (DraftPayload JSON)
    approved: bool  # a person approved what is saved
    status: str | None  # the receipt's status once saved
    saved: bool
    # The printed product code of each ITEM line of the saved extraction, {position, code}
    # (``reading_codes``); empty for a person's draft, whose codes are read from its lines.
    codes: list[dict[str, Any]]
    # The matching agent's decisions, one per matched line: {position, source, product_id,
    # new_product_name, code, name_source, catalog}, the ``save_matches`` tool's input.
    matches: list[dict[str, Any]]
    # The lines ``save_matches`` linked to a product created for them, {position, product_id,
    # catalog}, in line order: the products the enrichment agent describes, with the source
    # entry matching named each from (``catalog`` None or absent: none).
    created: list[dict[str, Any]]
    # What the enrichment agent describes, one per product: {product_id, fields, sources,
    # alias, store_key}, the ``describe_from_catalog`` tool's input.
    enrichments: list[dict[str, Any]]
    # Every product the receipt created, to look up or guess (``EnrichTarget`` as JSON):
    # {product_id, printed_text, store_name, store_key, department, code, gtin}.
    targets: list[dict[str, Any]]


def thread_id(receipt_id: int) -> str:
    """One graph thread per receipt; a review resumes it."""
    return f"receipt-{receipt_id}"
