"""Storage of receipts, products, prices and corrections (PLAN 3.4).

Repositories never commit: they are reached through a ``UnitOfWork``, which owns the
transaction. Methods that stamp times use the injected clock.
"""

from collections.abc import Sequence
from typing import Protocol

from paper_trail.domain.enums import CorrectionKind, ProcessingStage, ReceiptStatus
from paper_trail.domain.models import (
    Correction,
    ItemLink,
    PriceObservation,
    Product,
    ProductAlias,
    Receipt,
    ReceiptSummary,
)
from paper_trail.domain.paging import Page, PageRequest


class ReceiptRepository(Protocol):
    def add(self, receipt: Receipt) -> Receipt:
        """Store a new receipt; returns it with ids on it and on every item (items by position)."""
        ...

    def get(self, receipt_id: int) -> Receipt | None: ...

    def list(
        self,
        page: PageRequest,
        *,
        status: ReceiptStatus | None = None,
        store_key: str | None = None,
    ) -> Page[ReceiptSummary]:
        """Newest first (created_at desc, id desc). DomainValidationError on a bad cursor."""
        ...

    def update_status(self, receipt_id: int, status: ReceiptStatus, stage: ProcessingStage) -> None:
        """Set status and stage and stamp updated_at. NotFoundError for an unknown id."""
        ...

    def replace_contents(self, receipt: Receipt) -> Receipt:
        """Overwrite fields and items (items get new ids), keeping created_at and stamping
        updated_at; the replaced items' price observations go with them. NotFoundError for an
        unknown id."""
        ...

    def link_items(self, receipt_id: int, links: Sequence[ItemLink]) -> Receipt:
        """Set ``product_id`` and ``match_source`` of the lines at the links' positions, stamp
        updated_at and return the receipt; everything else, line ids included, is unchanged.
        NotFoundError (and no change) for an unknown receipt or position."""
        ...

    def find_by_image_sha256(self, sha256: str) -> Receipt | None:
        """The receipt of this image, if any. ``add`` raises ConflictError for a second one."""
        ...


class ProductRepository(Protocol):
    def add(self, product: Product) -> Product: ...

    def get(self, product_id: int) -> Product | None: ...

    def update(self, product: Product) -> Product:
        """Replace the stored product's name, brand, size, category and tags (enrichment);
        its id, ``created_at``, aliases, vector and prices are kept. Returns it as stored;
        NotFoundError (nothing changed) for an unknown id."""
        ...

    def find_by_alias(self, alias: str, store_key: str | None) -> Product | None:
        """Exact match on a normalized alias: the store's own alias first, then a global one.
        ``store_key=None`` looks at global aliases only."""
        ...

    def add_alias(self, alias: ProductAlias) -> ProductAlias:
        """Upsert on (alias, store_key). An existing USER_CORRECTION is kept (and returned)
        against any other source; otherwise the row is replaced in place (same id)."""
        ...

    def aliases(self, product_id: int) -> tuple[ProductAlias, ...]:
        """The product's aliases, ordered by id."""
        ...


class PriceRepository(Protocol):
    def add_many(self, observations: Sequence[PriceObservation]) -> tuple[PriceObservation, ...]:
        """Store observations, at most one per line item (ConflictError otherwise)."""
        ...

    def history(self, product_id: int) -> tuple[PriceObservation, ...]:
        """The product's observations, observed_on asc, then id asc."""
        ...

    def reassign(self, line_item_id: int, product_id: int) -> PriceObservation | None:
        """Move the line's observation to another product (all else kept); None when the line
        has none."""
        ...


class CorrectionRepository(Protocol):
    def add(self, correction: Correction) -> Correction: ...

    def latest(
        self, kind: CorrectionKind, subject: str, store_key: str | None
    ) -> Correction | None:
        """The newest correction (created_at, then id) for exactly this kind, subject and store
        (``None`` matches only corrections recorded without a store)."""
        ...
