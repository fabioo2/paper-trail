"""Product matching memory and the recording of matches (PLAN 6, agent core slice C).

``find_product`` is the remembered part of matching: a person's correction first, then an
exact alias. ``record_matches`` is what the supervisor's ``save_matches`` tool writes for the
matching agent's decisions, in one unit of work, idempotently: a run resumed after a crash
between that write and the graph's checkpoint saves the same decisions again and changes
nothing.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from paper_trail.application.products import new_product, price_observation, remember_alias
from paper_trail.domain.enums import (
    AliasSource,
    Category,
    CorrectionKind,
    LineKind,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
)
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import ItemLink, LineItem, Product, Receipt, require_id
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.ports.embeddings import Vector
from paper_trail.ports.unit_of_work import UnitOfWork

type RememberedSource = Literal[MatchSource.MEMORY, MatchSource.ALIAS]


def find_product(
    uow: UnitOfWork, alias: str, store_key: str | None
) -> tuple[Product, RememberedSource] | None:
    """The product a normalized alias names at a store, and how it was found: the newest
    PRODUCT_LINK correction for exactly (alias, store) first, then the store's alias, then a
    global one."""
    memory = uow.corrections.latest(CorrectionKind.PRODUCT_LINK, alias, store_key)
    if memory is not None and memory.new_value.isdigit():
        corrected = uow.products.get(int(memory.new_value))
        if corrected is not None:
            return corrected, MatchSource.MEMORY
    aliased = uow.products.find_by_alias(alias, store_key)
    return None if aliased is None else (aliased, MatchSource.ALIAS)


@dataclass(frozen=True, slots=True)
class MatchChoice:
    """The product of one receipt line: an existing one, or a new one named here (``NEW``)."""

    position: int
    source: MatchSource
    product_id: int | None = None
    new_product_name: str | None = None

    def __post_init__(self) -> None:
        if (self.product_id is None) == (self.new_product_name is None):
            msg = f"line {self.position}: give exactly one of a product id and a new product name"
            raise DomainValidationError(msg)
        if (self.source is MatchSource.NEW) != (self.new_product_name is not None):
            msg = f"line {self.position}: a new product is named exactly when the source is new"
            raise DomainValidationError(msg)


@dataclass(frozen=True, slots=True)
class MatchesRecorded:
    receipt_id: int
    links: tuple[ItemLink, ...]  # in the order of the choices
    new_products: int
    prices: int  # price observations recorded or moved


@dataclass(frozen=True, slots=True)
class _NewProduct:
    name: str
    category: Category | None


@dataclass(frozen=True, slots=True)
class MatchPlan:
    """What recording the choices needs, checked against the stored receipt."""

    receipt: Receipt
    lines: Mapping[int, LineItem]  # by position
    to_create: Mapping[str, _NewProduct]  # one product per line alias, in line order

    @property
    def aliases_to_embed(self) -> tuple[str, ...]:
        return tuple(self.to_create)


def plan_matches(uow: UnitOfWork, receipt_id: int, choices: Sequence[MatchChoice]) -> MatchPlan:
    """NotFoundError for an unknown receipt, position or product."""
    receipt = uow.receipts.get(receipt_id)
    if receipt is None:
        msg = f"receipt {receipt_id} does not exist"
        raise NotFoundError(msg)
    lines = {item.position: item for item in receipt.items}
    to_create: dict[str, _NewProduct] = {}
    for choice in choices:
        line = lines.get(choice.position)
        if line is None:
            msg = f"receipt {receipt_id} has no line at position {choice.position}"
            raise NotFoundError(msg)
        if choice.product_id is not None:
            if uow.products.get(choice.product_id) is None:
                msg = f"product {choice.product_id} does not exist"
                raise NotFoundError(msg)
        elif choice.new_product_name is not None and not _created_before(line):
            alias = normalize_alias(line.raw_text)
            to_create.setdefault(alias, _NewProduct(choice.new_product_name, line.category))
    return MatchPlan(receipt, lines, to_create)


def record_matches(
    uow: UnitOfWork,
    plan: MatchPlan,
    choices: Sequence[MatchChoice],
    vectors: Mapping[str, Vector],
    now: datetime,
) -> MatchesRecorded:
    """Create the new products (with their vectors), link the lines, remember each line's
    alias at the store (MATCHED), price every linked ITEM line when the date is known, and mark
    the receipt COMPLETED/DONE. ``vectors`` holds the embedding of each alias to create."""
    if set(vectors) != set(plan.to_create):
        msg = f"receipt {require_id(plan.receipt.id)} changed while its matches were being saved"
        raise ConflictError(msg)
    receipt_id = require_id(plan.receipt.id)
    created: dict[str, int] = {}
    for alias, wanted in plan.to_create.items():
        product_id = require_id(new_product(uow, wanted.name, wanted.category, now).id)
        uow.vectors.upsert(product_id, vectors[alias])
        created[alias] = product_id
    links = tuple(
        ItemLink(
            choice.position,
            _product_id(choice, plan.lines[choice.position], created),
            choice.source,
        )
        for choice in choices
    )
    linked = uow.receipts.link_items(receipt_id, links)
    positions = {link.position for link in links}
    prices = 0
    for item in linked.items:
        if item.position not in positions:
            continue
        product_id = require_id(item.product_id)
        alias = normalize_alias(item.raw_text)
        if alias:
            remember_alias(
                uow,
                product_id,
                alias,
                store_key=linked.store_key,
                source=AliasSource.MATCHED,
                now=now,
            )
        prices += _price(uow, linked, item, product_id)
    uow.receipts.update_status(receipt_id, ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    return MatchesRecorded(receipt_id, links, len(created), prices)


def _created_before(line: LineItem) -> bool:
    """The line already got a new product from an earlier save of the same decisions."""
    return line.product_id is not None and line.match_source is MatchSource.NEW


def _product_id(choice: MatchChoice, line: LineItem, created: Mapping[str, int]) -> int:
    if choice.product_id is not None:
        return choice.product_id
    if _created_before(line):
        return require_id(line.product_id)
    return created[normalize_alias(line.raw_text)]


def _price(uow: UnitOfWork, receipt: Receipt, item: LineItem, product_id: int) -> int:
    """Record (or, when the line has one, move) the line's price observation; how many."""
    if item.kind is not LineKind.ITEM or receipt.purchased_on is None:
        return 0
    if uow.prices.reassign(require_id(item.id), product_id) is None:
        uow.prices.add_many([price_observation(receipt, item, receipt.purchased_on)])
    return 1
