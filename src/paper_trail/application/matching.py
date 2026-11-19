"""Product matching memory and the recording of matches (PLAN 6, agent core slice C,
barcode-lookup plan 4.1).

``find_product`` is the remembered part of matching: a person's correction first, then an
exact alias at the store. ``record_matches`` is what the supervisor's ``save_matches`` tool
writes for the matching agent's decisions, in one unit of work, idempotently: a run resumed
after a crash between that write and the graph's checkpoint saves the same decisions again and
changes nothing.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from paper_trail.application.products import new_product, price_observation, remember_alias
from paper_trail.domain.enums import (
    AliasSource,
    Category,
    CodeSource,
    CorrectionKind,
    FieldSource,
    LineKind,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
)
from paper_trail.domain.errors import DomainValidationError, NotFoundError
from paper_trail.domain.models import (
    PERSON_CODE_SOURCES,
    CodeIdentity,
    Correction,
    ItemLink,
    LineItem,
    Product,
    ProductCode,
    Receipt,
    require_id,
)
from paper_trail.domain.rules.item_codes import ItemCode, parse_code_field, parse_item_code
from paper_trail.domain.rules.normalization import legacy_alias, normalize_alias
from paper_trail.ports.unit_of_work import UnitOfWork

type RememberedSource = Literal[MatchSource.MEMORY, MatchSource.ALIAS]


type NewProductKey = CodeIdentity | str  # a coded line's code identity, else the line's alias

# Where matching may take a new product's name from: never a person (that is ``items correct``).
NEW_NAME_SOURCES = frozenset({FieldSource.RECEIPT_TEXT, FieldSource.PLU})
# Aliases a person taught (an import or review answer, a correction) beat a machine's.
PERSON_ALIASES = frozenset({AliasSource.USER_CORRECTION, AliasSource.IMPORT})


def latest_correction(
    uow: UnitOfWork, kind: CorrectionKind, text: str, store_key: str | None
) -> Correction | None:
    """The newest correction (created_at, then id) of ``kind`` at exactly ``store_key`` for a
    line's text, under its alias (``normalize_alias``) or the alias it had before the tax-flag
    change (``legacy_alias``)."""
    found = [
        correction
        for alias in _alias_keys(text)
        if (correction := uow.corrections.latest(kind, alias, store_key)) is not None
    ]
    return max(found, key=lambda row: (row.created_at, require_id(row.id)), default=None)


def find_product(
    uow: UnitOfWork, text: str, store_key: str | None
) -> tuple[Product, RememberedSource] | None:
    """The product a line's text (raw or already normalized) names at a store, and how it was
    found: the newest PRODUCT_LINK correction for its alias at the store
    (``latest_correction``), then an alias at exactly that store (``None``: global aliases
    only), under either key: a person's alias (USER_CORRECTION, IMPORT) before a machine's,
    then the newest. Another store's alias and a global one never name a product at a
    store."""
    memory = latest_correction(uow, CorrectionKind.PRODUCT_LINK, text, store_key)
    if memory is not None and memory.new_value.isdigit():
        corrected = uow.products.get(int(memory.new_value))
        if corrected is not None:
            return corrected, MatchSource.MEMORY
    aliases = [
        alias
        for key in _alias_keys(text)
        if (alias := uow.products.alias_at(key, store_key)) is not None
    ]
    best = max(
        aliases,
        key=lambda row: (row.source in PERSON_ALIASES, row.created_at, require_id(row.id)),
        default=None,
    )
    aliased = None if best is None else uow.products.get(best.product_id)
    return None if aliased is None else (aliased, MatchSource.ALIAS)


def _alias_keys(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((normalize_alias(text), legacy_alias(text))))


@dataclass(frozen=True, slots=True)
class MatchChoice:
    """The product of one receipt line: an existing one, or a new one named here (``NEW``).

    ``code`` is the line's code identity when it was matched by its code (CATALOG, a MEMORY
    correction of a coded line, or NEW from a coded line); None for an uncoded line or with
    the catalog off. ``name_source`` is where a NEW product's name came from (None:
    ``receipt_text``); only NEW takes one. ``printed_code`` is the code a reading gave the line
    (``parse_code_field``), when ``code`` came from it rather than from the line's raw text.
    ``category_source`` is where the line's own
    category came from (None: ``person``, a person's draft): ``receipt_text`` for a printed
    department heading."""

    position: int
    source: MatchSource
    product_id: int | None = None
    new_product_name: str | None = None
    code: CodeIdentity | None = None
    name_source: FieldSource | None = None
    category_source: FieldSource | None = None
    printed_code: str | None = None

    def __post_init__(self) -> None:
        if (self.product_id is None) == (self.new_product_name is None):
            msg = f"line {self.position}: give exactly one of a product id and a new product name"
            raise DomainValidationError(msg)
        if (self.source is MatchSource.NEW) != (self.new_product_name is not None):
            msg = f"line {self.position}: a new product is named exactly when the source is new"
            raise DomainValidationError(msg)
        if self.name_source is not None and self.source is not MatchSource.NEW:
            msg = f"line {self.position}: only a new product takes a name source"
            raise DomainValidationError(msg)
        if self.name_source is not None and self.name_source not in NEW_NAME_SOURCES:
            msg = (
                f"line {self.position}: a new product is named by its printed text or the PLU"
                f" table, not {self.name_source.value}"
            )
            raise DomainValidationError(msg)


@dataclass(frozen=True, slots=True)
class MatchesRecorded:
    """``unpriced``: the linked ITEM lines with a negative total (possible only on a receipt a
    person forced through), which get no price observation, a line the review answer linked
    included."""

    receipt_id: int
    links: tuple[ItemLink, ...]  # in the order of the choices
    new_products: int
    prices: int  # price observations recorded or moved
    unpriced: tuple[LineItem, ...]


@dataclass(frozen=True, slots=True)
class _NewProduct:
    name: str
    category: Category | None
    field_sources: Mapping[str, FieldSource]
    code: CodeIdentity | None  # attached with the machine policy
    code_source: CodeSource


@dataclass(frozen=True, slots=True)
class _Link:
    """How one choice is written, re-resolved against the stored codes."""

    source: MatchSource
    product_id: int | None  # an existing product, or None for ``new_key``'s
    new_key: NewProductKey | None = None
    attach: tuple[CodeIdentity, CodeSource] | None = None  # the code, attached to the product
    remember_alias: bool = False  # uncoded lines are remembered by their alias


@dataclass(frozen=True, slots=True)
class MatchPlan:
    """What recording the choices needs, checked against the stored receipt and its codes."""

    receipt: Receipt
    lines: Mapping[int, LineItem]  # by position
    # One product per code identity for coded lines, per alias for uncoded ones, in line order.
    to_create: Mapping[NewProductKey, _NewProduct]
    links: Mapping[int, _Link]  # by position


@dataclass(frozen=True, slots=True)
class _Coded:
    """A choice for a line that carries a code, with the line and its parsed code."""

    choice: MatchChoice
    line: LineItem
    code: ItemCode


def plan_matches(uow: UnitOfWork, receipt_id: int, choices: Sequence[MatchChoice]) -> MatchPlan:
    """NotFoundError for an unknown receipt or position; DomainValidationError for a ``code``
    that is not the identity of the stored line's code at the receipt's store. A choice naming a
    product that no longer exists is decided again (``_redecided``; NotFoundError when nothing
    names a product for its line).

    The choices for one code are resolved together against ``product_codes`` as it is now,
    inside the writing unit of work, so every line with that code ends on one product, whatever
    the line order: the product a person attached the code to; else the one the newest of the
    lines' corrections names; else the product the code names; else the product a CATALOG
    choice named (the code attached to it again); else one new product. Only a line whose own
    correction names that product is MEMORY. A line that an earlier save of the same choices
    linked to a product created for it keeps it (NEW) only while the code names that product,
    or names none (the code is attached to it again)."""
    receipt = uow.receipts.get(receipt_id)
    if receipt is None:
        msg = f"receipt {receipt_id} does not exist"
        raise NotFoundError(msg)
    lines = {item.position: item for item in receipt.items}
    groups: dict[CodeIdentity, list[_Coded]] = {}
    current: list[MatchChoice] = []
    for decided in choices:
        line = lines.get(decided.position)
        if line is None:
            msg = f"receipt {receipt_id} has no line at position {decided.position}"
            raise NotFoundError(msg)
        choice = decided
        if choice.product_id is not None and uow.products.get(choice.product_id) is None:
            choice = _redecided(uow, choice, line, receipt.store_key)
        current.append(choice)
        if choice.code is not None:
            code = _checked_code(choice, choice.code, line, receipt.store_key)
            groups.setdefault(choice.code, []).append(_Coded(choice, line, code))
    to_create: dict[NewProductKey, _NewProduct] = {}
    links: dict[int, _Link] = {}
    for choice in current:
        if choice.position in links:
            continue
        if choice.code is not None:
            members = groups[choice.code]
            links |= _group_links(uow, receipt.store_key, choice.code, members, to_create)
            continue
        line = lines[choice.position]
        if choice.source is MatchSource.NEW and _created_before(line):
            links[choice.position] = _Link(MatchSource.NEW, line.product_id)
        elif choice.new_product_name is not None:
            alias = normalize_alias(line.raw_text)
            if alias not in to_create:
                to_create[alias] = _new_product(uow, choice, line, receipt.store_key)
            links[choice.position] = _Link(MatchSource.NEW, None, alias, remember_alias=True)
        else:
            links[choice.position] = _Link(choice.source, choice.product_id, remember_alias=True)
    return MatchPlan(receipt, lines, to_create, links)


def _redecided(uow: UnitOfWork, choice: MatchChoice, line: LineItem, store_key: str) -> MatchChoice:
    """A decision whose product no longer exists (it was merged into another while the run was
    paused), decided again as matching decides without a model: a coded line by its code's
    product (CATALOG), else a person's correction for it (MEMORY); any other line by a person's
    correction, then the store's alias (``find_product``). NotFoundError naming the product
    when none of them names one."""
    if choice.code is not None:
        stored = stored_code(uow, _checked_code(choice, choice.code, line, store_key), choice.code)
        if stored is not None:
            return replace(choice, source=MatchSource.CATALOG, product_id=stored.product_id)
    found = find_product(uow, line.raw_text, store_key)
    if found is None or (choice.code is not None and found[1] is not MatchSource.MEMORY):
        msg = f"product {choice.product_id} does not exist"
        raise NotFoundError(msg)
    product, source = found
    return replace(choice, source=source, product_id=require_id(product.id))


def _checked_code(
    choice: MatchChoice, identity: CodeIdentity, line: LineItem, store_key: str
) -> ItemCode:
    """The line's code, which must be the choice's: the code its reading gave it, else the one
    its raw text starts with."""
    parsed = (
        parse_item_code(line.raw_text, store_key)
        if choice.printed_code is None
        else parse_code_field(choice.printed_code, store_key)
    )
    if parsed is None or parsed.identity_at(store_key) != identity:
        msg = (
            f"line {choice.position}: {identity.scheme.value} {identity.store_key}"
            f" {identity.code} is not the code of this line"
        )
        raise DomainValidationError(msg)
    return parsed


def _group_links(
    uow: UnitOfWork,
    store_key: str,
    identity: CodeIdentity,
    members: Sequence[_Coded],
    to_create: dict[NewProductKey, _NewProduct],
) -> dict[int, _Link]:
    """One target product for every line of one code (see ``plan_matches``), chosen without
    regard to line order; a line keeps MEMORY only when its own correction names it."""
    stored = stored_code(uow, members[0].code, identity)
    person_row = stored is not None and stored.source in PERSON_CODE_SOURCES
    target = (
        stored.product_id
        if stored is not None and person_row
        else _corrected_target(uow, store_key, members)
    )
    if target is not None:
        taught = not person_row
        keeps = [m.choice.position for m in members if _corrects_to(m.choice, target)]
        return {
            m.choice.position: _Link(
                MatchSource.MEMORY if m.choice.position in keeps else MatchSource.CATALOG,
                target,
                attach=(identity, CodeSource.CORRECTION)
                if taught and m.choice.position == keeps[0]
                else None,
            )
            for m in members
        }
    if stored is not None:
        return {
            m.choice.position: _Link(
                MatchSource.NEW if _created(m, stored.product_id) else MatchSource.CATALOG,
                stored.product_id,
            )
            for m in members
        }
    catalog = next((m.choice for m in members if m.choice.source is MatchSource.CATALOG), None)
    if catalog is not None:
        target = require_id(catalog.product_id)
        attach = (identity, CodeSource.RECEIPT)
        return {
            m.choice.position: _Link(
                MatchSource.CATALOG, target, attach=attach if m.choice is catalog else None
            )
            for m in members
        }
    replayed = next((m.line for m in members if _created(m, m.line.product_id)), None)
    if replayed is not None:
        attach = (identity, CodeSource.RECEIPT)
        first = members[0].choice.position
        return {
            m.choice.position: _Link(
                MatchSource.NEW,
                replayed.product_id,
                attach=attach if m.choice.position == first else None,
            )
            for m in members
        }
    named = next((m for m in members if m.choice.new_product_name is not None), None)
    if named is None:
        return {m.choice.position: _Link(m.choice.source, m.choice.product_id) for m in members}
    to_create[identity] = _new_product(uow, named.choice, named.line, store_key)
    return {m.choice.position: _Link(MatchSource.NEW, None, identity) for m in members}


def _corrects_to(choice: MatchChoice, product_id: int) -> bool:
    return choice.source is MatchSource.MEMORY and choice.product_id == product_id


def _corrected_target(uow: UnitOfWork, store_key: str, members: Sequence[_Coded]) -> int | None:
    """The product the newest of the group's corrections names (a person's PRODUCT_LINK
    correction for a member's line at the store, created_at then id); ties and decisions
    without a stored correction fall back to the lowest product id. None without a MEMORY
    choice."""
    ranked: list[tuple[tuple[datetime, int] | None, int]] = []
    for member in members:
        choice = member.choice
        if choice.source is not MatchSource.MEMORY or choice.product_id is None:
            continue
        found = latest_correction(uow, CorrectionKind.PRODUCT_LINK, member.line.raw_text, store_key)
        when = (
            (found.created_at, require_id(found.id))
            if found is not None and found.new_value == str(choice.product_id)
            else None
        )
        ranked.append((when, choice.product_id))
    if not ranked:
        return None
    dated = [entry for entry in ranked if entry[0] is not None]
    if dated:
        return max(dated, key=lambda entry: (entry[0], -entry[1]))[1]
    return min(product_id for _, product_id in ranked)


def _created(member: _Coded, product_id: int | None) -> bool:
    """An earlier save of the same choices created ``product_id`` for this line."""
    line = member.line
    return (
        member.choice.source is MatchSource.NEW
        and _created_before(line)
        and line.product_id == product_id
    )


def stored_code(uow: UnitOfWork, code: ItemCode, identity: CodeIdentity) -> ProductCode | None:
    """The row that names the code (its identities in order), if any."""
    for candidate in code.identities:
        found = uow.product_codes.find(identity.scheme, identity.store_key, candidate)
        if found is not None:
            return found
    return None


def _new_product(
    uow: UnitOfWork, choice: MatchChoice, line: LineItem, store_key: str
) -> _NewProduct:
    """The name's source as decided; the category a person gave the line (a correction at the
    store, ``person``), else the line's own with its source (``category_source``: a printed
    department heading is ``receipt_text``, a person's draft ``person``), never a model's."""
    name_source = choice.name_source or FieldSource.RECEIPT_TEXT
    sources: dict[str, FieldSource] = {"canonical_name": name_source}
    correction = latest_correction(uow, CorrectionKind.CATEGORY, line.raw_text, store_key)
    category: Category | None
    if correction is not None and correction.new_value in Category:
        category, category_source = Category(correction.new_value), FieldSource.PERSON
    else:
        category, category_source = line.category, choice.category_source or FieldSource.PERSON
    if category is not None:
        sources["category"] = category_source
    return _NewProduct(
        str(choice.new_product_name),
        category,
        sources,
        choice.code,
        CodeSource.PLU if name_source is FieldSource.PLU else CodeSource.RECEIPT,
    )


def record_matches(
    uow: UnitOfWork,
    plan: MatchPlan,
    choices: Sequence[MatchChoice],
    now: datetime,
) -> MatchesRecorded:
    """Create the new products, link the lines, attach the codes, remember each uncoded line's
    alias at the store (MATCHED), price every linked ITEM line when the date is known (one with
    a negative total is left unpriced: a price cannot be negative), and mark the receipt
    COMPLETED/DONE (``plan`` is made in this unit, so it sees the codes as they are now).

    A new product from a coded line gets its code (``plu`` when a source named it, else
    ``receipt``; machine policy), a person's correction of a coded line attaches the code to the
    corrected product (``correction``; person policy), a CATALOG choice whose code was removed
    meanwhile attaches it again (``receipt``), and nothing else attaches one."""
    receipt_id = require_id(plan.receipt.id)
    created: dict[NewProductKey, int] = {}
    for key, wanted in plan.to_create.items():
        product_id = require_id(
            new_product(
                uow, wanted.name, wanted.category, now, field_sources=wanted.field_sources
            ).id
        )
        if wanted.code is not None:
            attach_code(uow, wanted.code, product_id, wanted.code_source, now)
        created[key] = product_id
    resolved = [plan.links[choice.position] for choice in choices]
    links = tuple(
        ItemLink(
            choice.position,
            require_id(link.product_id) if link.new_key is None else created[link.new_key],
            link.source,
        )
        for choice, link in zip(choices, resolved, strict=True)
    )
    linked = uow.receipts.link_items(receipt_id, links)
    for item_link, link in zip(links, resolved, strict=True):
        if link.attach is not None:
            code, source = link.attach
            attach_code(uow, code, item_link.product_id, source, now)
    remembered = {
        item_link.position
        for item_link, link in zip(links, resolved, strict=True)
        if link.remember_alias
    }
    positions = {link.position for link in links}
    prices = 0
    unpriced: list[LineItem] = []
    for item in linked.items:
        negative = item.kind is LineKind.ITEM and item.line_total.is_negative()
        if negative and item.product_id is not None:
            unpriced.append(item)
        if item.position not in positions:
            continue
        product_id = require_id(item.product_id)
        alias = normalize_alias(item.raw_text)
        if alias and item.position in remembered:
            remember_alias(
                uow,
                product_id,
                alias,
                store_key=linked.store_key,
                source=AliasSource.MATCHED,
                now=now,
            )
        if not negative:
            prices += _price(uow, linked, item, product_id)
    uow.receipts.update_status(receipt_id, ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    return MatchesRecorded(receipt_id, links, len(created), prices, tuple(unpriced))


def attach_code(
    uow: UnitOfWork, code: CodeIdentity, product_id: int, source: CodeSource, now: datetime
) -> ProductCode:
    """Upsert ``code`` for the product: a person's source replaces another product's mapping, a
    machine's never does (``ProductCodeRepository.upsert``)."""
    return uow.product_codes.upsert(
        ProductCode(
            id=None,
            product_id=product_id,
            scheme=code.scheme,
            store_key=code.store_key,
            code=code.code,
            source=source,
            created_at=now,
            updated_at=now,
        ),
        replace=source in PERSON_CODE_SOURCES,
    )


def _created_before(line: LineItem) -> bool:
    """The line already got a new product from an earlier save of the same decisions."""
    return line.product_id is not None and line.match_source is MatchSource.NEW


def _price(uow: UnitOfWork, receipt: Receipt, item: LineItem, product_id: int) -> int:
    """Record (or, when the line has one, move) the line's price observation; how many."""
    if item.kind is not LineKind.ITEM or receipt.purchased_on is None:
        return 0
    if uow.prices.reassign(require_id(item.id), product_id) is None:
        uow.prices.add_many([price_observation(receipt, item, receipt.purchased_on)])
    return 1
