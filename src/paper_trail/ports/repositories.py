"""Storage of receipts, products, prices and corrections (PLAN 3.4).

Repositories never commit: they are reached through a ``UnitOfWork``, which owns the
transaction. Methods that stamp times use the injected clock.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol

from paper_trail.domain.enums import CodeScheme, CorrectionKind, ProcessingStage, ReceiptStatus
from paper_trail.domain.models import (
    Correction,
    ItemLink,
    PriceObservation,
    Product,
    ProductAlias,
    ProductCode,
    ProductLine,
    Receipt,
    ReceiptSummary,
)
from paper_trail.domain.money import Money
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

    def set_discount_links(self, receipt_id: int, links: Mapping[int, int]) -> Receipt:
        """Set ``applies_to_position`` of the discount lines at the keys to the item positions
        they map to, stamp updated_at and return the receipt; line ids and everything else are
        unchanged. A link already equal is left as it is: when every link is, nothing is
        written and updated_at stays. ConflictError (and no change) when a line already links
        elsewhere; NotFoundError (and no change) for an unknown receipt, or a position on either
        side that names no line; DomainValidationError for a link the domain refuses."""
        ...

    def item_lines(self, product_id: int) -> tuple[ProductLine, ...]:
        """The ITEM lines linked to the product, earliest first: by the receipt's
        ``purchased_on`` (undated receipts last), then receipt id, then position."""
        ...


class ProductRepository(Protocol):
    def add(self, product: Product) -> Product: ...

    def get(self, product_id: int) -> Product | None: ...

    def list(self) -> tuple[Product, ...]:
        """Every product, by id."""
        ...

    def update(self, product: Product) -> Product:
        """Replace the stored product's name, brand, size, category, tags and field sources;
        its id, ``created_at``, aliases, codes and prices are kept. Returns it as
        stored; NotFoundError (nothing changed) for an unknown id."""
        ...

    def find_by_alias(self, alias: str, store_key: str | None) -> Product | None:
        """Exact match on a normalized alias: the store's own alias first, then a global one.
        ``store_key=None`` looks at global aliases only."""
        ...

    def alias_at(self, alias: str, store_key: str | None) -> ProductAlias | None:
        """The alias stored under exactly (alias, store_key); ``None`` is a key too (global)."""
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

    def for_line_item(self, line_item_id: int) -> PriceObservation | None:
        """The line's observation, if it has one."""
        ...

    def set_discount(self, line_item_id: int, discount: Money) -> PriceObservation | None:
        """Set the line's observation's discount (its paid and unit discount follow) and return
        it; None, and nothing written, when the line has no observation."""
        ...


class CorrectionRepository(Protocol):
    def add(self, correction: Correction) -> Correction: ...

    def latest(
        self, kind: CorrectionKind, subject: str, store_key: str | None
    ) -> Correction | None:
        """The newest correction (created_at, then id) for exactly this kind, subject and store
        (``None`` matches only corrections recorded without a store)."""
        ...


class ProductCodeRepository(Protocol):
    """``product_codes``: a code names at most one product (unique on scheme, store, code)."""

    def find(self, scheme: CodeScheme, store_key: str, code: str) -> ProductCode | None:
        """Exact on all three parts (no zero-stripping, ``""`` is the store of GTIN and PLU)."""
        ...

    def for_product(self, product_id: int) -> tuple[ProductCode, ...]:
        """The product's codes, ordered by id."""
        ...

    def list(
        self, *, store_key: str | None = None, scheme: CodeScheme | None = None
    ) -> tuple[ProductCode, ...]:
        """Every code, filtered by store and scheme when given, ordered by id."""
        ...

    def upsert(self, code: ProductCode, *, replace: bool) -> ProductCode:
        """Store ``code`` (its ``id`` is ignored) and return the row as stored.

        No row yet: inserted with the code's ``created_at`` and ``updated_at``. A row naming the
        same product: unchanged when the source is the same or ``replace`` is false; else its
        source and ``updated_at`` are updated. A row naming another product: with ``replace``
        (a person's source) it is moved to the code's product with its source and
        ``updated_at`` (``created_at`` kept); without, nothing is written and one
        ``catalog.code_conflict`` warning is logged with scheme, store_key, code,
        existing_product_id and refused_product_id."""
        ...

    def remove(self, scheme: CodeScheme, store_key: str, code: str) -> bool:
        """Delete the code (its product is kept); False when there was none."""
        ...
