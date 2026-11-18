"""Body of the search across products and receipts."""

from pydantic import BaseModel

from paper_trail.application.search import SearchResults
from paper_trail.domain.models import ReceiptMatch
from paper_trail.interfaces.web.schemas.products import ProductSummaryOut
from paper_trail.interfaces.web.schemas.receipts import ReceiptSummaryOut


class ReceiptMatchOut(ReceiptSummaryOut):
    """A receipt found by a search: ``lines`` are the descriptions of its lines that matched
    (empty when only the store name did)."""

    lines: list[str]

    @classmethod
    def from_match(cls, match: ReceiptMatch) -> "ReceiptMatchOut":
        return cls(
            **ReceiptSummaryOut.from_domain(match.receipt).model_dump(), lines=list(match.lines)
        )


class SearchOut(BaseModel):
    products: list[ProductSummaryOut]
    receipts: list[ReceiptMatchOut]

    @classmethod
    def from_results(cls, results: SearchResults) -> "SearchOut":
        return cls(
            products=[ProductSummaryOut.from_summary(summary) for summary in results.products],
            receipts=[ReceiptMatchOut.from_match(match) for match in results.receipts],
        )
