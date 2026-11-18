"""One search across the products bought and the receipts: products by name, brand, tag or
category, receipts by store name or by a line's printed text, description or product."""

from dataclasses import dataclass

from paper_trail.domain.enums import Category
from paper_trail.domain.models import ProductSummary, ReceiptMatch, require_id
from paper_trail.ports.unit_of_work import UnitOfWorkFactory

PRODUCT_RESULTS = 10
RECEIPT_RESULTS = 20


@dataclass(frozen=True, slots=True)
class SearchResults:
    products: tuple[ProductSummary, ...]
    receipts: tuple[ReceiptMatch, ...]


class SearchPurchases:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(self, query: str) -> SearchResults:
        """Products bought, best text match first, then those of a matching category by name
        (at most ``PRODUCT_RESULTS``); receipts newest purchase first (at most
        ``RECEIPT_RESULTS``). Every word must match as a word prefix, ignoring case. Read-only."""
        needle = query.strip()
        if not needle:
            return SearchResults((), ())
        words = needle.lower().split()
        categories = {
            category
            for category in Category
            if all(
                any(part.startswith(word) for part in category.value.split("_")) for word in words
            )
        }
        with self._uow_factory(read_only=True) as uow:
            named = uow.text_search.product_ids(needle)
            bought = {require_id(s.product.id): s for s in uow.products.summaries()}
            receipts = uow.receipts.matching(needle, limit=RECEIPT_RESULTS)
        in_category = [pid for pid, s in bought.items() if s.product.category in categories]
        found = [pid for pid in dict.fromkeys([*named, *in_category]) if pid in bought]
        return SearchResults(
            products=tuple(bought[pid] for pid in found[:PRODUCT_RESULTS]), receipts=receipts
        )
