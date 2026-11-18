"""Describing products from catalogs, databases and guesses (barcode-lookup plan 4.2,
ADR-0018; claude-extraction plan V3), and the ``catalog`` commands (plan section 5).

``describe_product`` is what the supervisor's ``describe_from_catalog`` tool and enrichment
write: only the fields that come with a source, only over an empty, printed or guessed field
(``field_is_open``), and a person's category for the line at that store over any source's. It
reads the product again inside its unit of work, so a person's write between the caller's read
and this one is kept.

The commands: ``CatalogTodo`` lists the products nobody has confirmed yet (printed or guessed
names, coded or not), ``ConfirmProduct`` makes a product's values a person's, ``EditProduct``
writes the values a person typed, ``AddCatalogCode`` lets a person name one (or point its code
at an existing product), ``ListCatalogCodes``, ``RemoveCatalogCode`` and ``LookupCatalogCode``
show and fix ``product_codes``, and ``import_plu`` writes ``plu.csv`` from an IFPS export. None
of them calls a model or opens a network connection (``catalog enrich`` is
``application.enrichment``).
"""

import csv
import os
import re
import tempfile
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

import structlog

from paper_trail.application.matching import (
    attach_code,
    latest_correction,
    stored_code,
)
from paper_trail.application.payloads import (
    FieldSourceKey,
    ProductFieldsPayload,
)
from paper_trail.domain.enums import (
    Category,
    CodeScheme,
    CodeSource,
    CorrectionKind,
    FieldSource,
    Unit,
)
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import (
    OPEN_SOURCES,
    CodeIdentity,
    Product,
    ProductCode,
    is_confirmed,
    require_id,
)
from paper_trail.domain.money import Money
from paper_trail.domain.rules.item_codes import (
    PLU_RANGE,
    ItemCode,
    lookup_url,
    parse_bare_code,
    parse_item_code,
)
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.domain.rules.normalization import (
    normalize_store_name,
)
from paper_trail.ports.catalog import CatalogProduct, ProductCatalog
from paper_trail.ports.clock import Clock
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger(__name__)

PLU_COLUMNS = ("code", "name", "organic")
PLU_EXPORT_CODE = "plu"
PLU_EXPORT_COMMODITY = "commodity"
PLU_EXPORT_VARIETY = "variety"
PLU_EXPORT_SIZE = "size"
PLU_EXPORT_CATEGORY = "category"
RETAILER_ASSIGNED_CATEGORY = "Retailer Assigned Numbers"
RETAILER_ASSIGNED_VARIETY = "Retailer Assigned"
ALL_SIZES = "All Sizes"
_PLU = re.compile(r"[0-9]{4}")
_SIZE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)")


def describe_product(  # noqa: PLR0913 - the product, its fields and sources, and the line
    uow_factory: UnitOfWorkFactory,
    product_id: int,
    *,
    fields: ProductFieldsPayload,
    sources: Mapping[FieldSourceKey, FieldSource],
    alias: str,
    store_key: str,
) -> Product:
    """Write ``fields`` with their ``sources`` onto the product, in one unit of work, and return
    it as stored. Only an empty field or one sourced ``receipt_text`` or ``llm`` is written
    (``OPEN_SOURCES``, per field); a category from a printed department heading
    (``receipt_text``) is never replaced by a guess (``llm``). The newest
    CATEGORY correction for ``alias`` (a line's raw text, both normalizations tried) at
    ``store_key`` sets the category with source ``person``, given a category or not. Free text
    is masked. Nothing is written when nothing changes. NotFoundError (nothing written) for an
    unknown product."""
    with uow_factory() as uow:
        product = uow.products.get(product_id)
        if product is None:
            msg = f"product {product_id} does not exist"
            raise NotFoundError(msg)
        correction = latest_correction(uow, CorrectionKind.CATEGORY, alias, store_key)
        remembered = (
            Category(correction.new_value)
            if correction is not None and correction.new_value in Category
            else None
        )
        described = _described(product, fields, sources, remembered)
        if described == product:
            return product
        stored = uow.products.update(described)
        uow.commit()
    return stored


def _described(
    product: Product,
    fields: ProductFieldsPayload,
    sources: Mapping[FieldSourceKey, FieldSource],
    remembered: Category | None,
) -> Product:
    """``product`` with every sourced field written where the stored one is still open."""
    written = dict(product.field_sources)
    described = product

    def open_for(key: FieldSourceKey) -> bool:
        if key not in sources or not field_is_open(written, key, sources[key]):
            return False
        written[key] = sources[key]
        return True

    if fields.name is not None and open_for("canonical_name"):
        described = replace(described, canonical_name=mask_pans(fields.name).text)
    if fields.brand is not None and open_for("brand"):
        described = replace(described, brand=mask_pans(fields.brand).text)
    if fields.size_value is not None and open_for("size"):
        described = replace(
            described, size_value=Decimal(fields.size_value), size_unit=fields.size_unit
        )
    if remembered is not None:
        if written.get("category") is not FieldSource.PERSON:
            described = replace(described, category=remembered)
            written["category"] = FieldSource.PERSON
    elif fields.category is not None and open_for("category"):
        described = replace(described, category=fields.category)
    if fields.tags is not None and open_for("tags"):
        described = replace(described, tags=tuple(mask_pans(tag).text for tag in fields.tags))
    return replace(described, field_sources=written)


def field_is_open(
    stored: Mapping[str, FieldSource], key: FieldSourceKey, incoming: FieldSource
) -> bool:
    """Whether enrichment may write ``key`` from ``incoming`` over what is stored: the field is
    empty or sourced ``receipt_text``/``llm``, and a guess never replaces a category read from
    a printed department heading."""
    current = stored.get(key)
    if current is None:
        return True
    if current not in OPEN_SOURCES:
        return False
    return not (
        key == "category" and current is FieldSource.RECEIPT_TEXT and incoming is FieldSource.LLM
    )


def product_code_at(store_key: str, code: str) -> ItemCode:
    """A bare code read with the store's profile (``parse_bare_code``) that names a product.
    DomainValidationError ``{code!r} is not a product code at {store_key}`` when it is not only
    a code, or reads as no product code (kind NONE: a coupon, a bad check digit, too short)."""
    parsed = parse_bare_code(code, store_key)
    if parsed is None or parsed.identity_at(store_key) is None:
        msg = f"{code!r} is not a product code at {store_key}"
        raise DomainValidationError(msg)
    return parsed


def parse_size(text: str) -> tuple[Decimal, Unit]:
    """A size as a person types it: a positive number and a ``Unit`` value, with or without a
    space (``1.36 kg``, ``1.36kg``, ``500 ml``, ``12 each``); the unit ignores case.
    DomainValidationError ``size {text!r}: give a positive number and one of {units}``."""
    match = _SIZE.fullmatch(text.strip())
    if match is None or match[2].casefold() not in Unit or Decimal(match[1]) <= 0:
        units = ", ".join(Unit)
        msg = f"size {text!r}: give a positive number and one of {units}"
        raise DomainValidationError(msg)
    return Decimal(match[1]), Unit(match[2].casefold())


@dataclass(frozen=True, slots=True)
class TodoRow:
    """A product nobody has confirmed yet (its name printed or guessed), coded or not, and how
    it was bought (at the asked store only, when one was asked). ``name_source`` is where its
    name came from (``llm``: a guess). ``raw_text`` is its earliest line as printed. ``code``
    is its first code (None: uncoded). ``last_paid``/``last_date`` are the newest price
    observation's paid price and date (None when its lines have none). ``lookup_url`` is the
    store's search link for the code, never requested here."""

    product_id: int
    name: str
    name_source: FieldSource | None
    raw_text: str
    code: CodeIdentity | None
    times_bought: int
    last_paid: Money | None
    last_date: date | None
    lookup_url: str | None


class CatalogTodo:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(
        self, *, store: str | None = None, limit: int | None = None
    ) -> tuple[TodoRow, ...]:
        """Every unconfirmed product (``is_confirmed`` false) that has ITEM lines, one row
        each: most lines first, then the newest ``last_date``, then the product id. ``store``
        (normalized with ``normalize_store_name``) keeps the products bought there and counts
        only those lines; ``limit`` keeps the first rows. Read-only."""
        store_key = None if store is None else normalize_store_name(store)
        rows: list[TodoRow] = []
        with self._uow_factory(read_only=True) as uow:
            for product in uow.products.list():
                product_id = require_id(product.id)
                if is_confirmed(product):
                    continue
                lines = [
                    line
                    for line in uow.receipts.item_lines(product_id)
                    if store_key is None or line.store_key == store_key
                ]
                if not lines:
                    continue
                history = [
                    observation
                    for observation in uow.prices.history(product_id)
                    if store_key is None or observation.store_key == store_key
                ]
                newest = history[-1] if history else None
                codes = uow.product_codes.for_product(product_id)
                code = codes[0] if codes else None
                rows.append(
                    TodoRow(
                        product_id=product_id,
                        name=product.canonical_name,
                        name_source=product.field_sources.get("canonical_name"),
                        raw_text=lines[0].line.raw_text,
                        code=None if code is None else code.identity,
                        times_bought=len(lines),
                        last_paid=None if newest is None else newest.paid,
                        last_date=None if newest is None else newest.observed_on,
                        lookup_url=None if code is None else lookup_url(code.store_key, code.code),
                    )
                )
        rows.sort(
            key=lambda row: (
                -row.times_bought,
                row.last_date is None,
                -(row.last_date or date.min).toordinal(),
                row.product_id,
            )
        )
        return tuple(rows if limit is None else rows[:limit])


type ShownValue = str | Sequence[str] | None


def shown_value(product: Product, key: FieldSourceKey) -> ShownValue:
    """A field as the product endpoints show it: the size as ``<value> <unit>`` (``24 each``,
    the value in fixed point as the API writes it), the category's value, the tags as a tuple;
    None when empty."""
    match key:
        case "canonical_name":
            return product.canonical_name
        case "brand":
            return product.brand
        case "size":
            return (
                None
                if product.size_value is None or product.size_unit is None
                else f"{product.size_value:f} {product.size_unit}"
            )
        case "category":
            return None if product.category is None else product.category.value
        case "tags":
            return product.tags or None


def _shown(value: ShownValue) -> ShownValue:
    """``value`` as ``shown_value`` gives it: tags as a tuple, no tags as None."""
    if value is None or isinstance(value, str):
        return value
    return tuple(value) or None


def _refuse_changed(product: Product, expected: Mapping[FieldSourceKey, ShownValue] | None) -> None:
    """ConflictError naming each field whose stored value is no longer the ``expected`` one."""
    changed = sorted(
        key for key, value in (expected or {}).items() if shown_value(product, key) != _shown(value)
    )
    if changed:
        msg = f"product {product.id} changed since it was shown: {', '.join(changed)}"
        raise ConflictError(msg)


class ConfirmProduct:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(
        self,
        product_id: int,
        fields: Collection[FieldSourceKey] | None = None,
        expected: Mapping[FieldSourceKey, ShownValue] | None = None,
    ) -> Product:
        """Stamp every filled field of the product ``person``, values unchanged (an empty field
        stays empty and unstamped, so enrichment may still fill it), in one unit of work, and
        return it as stored. ``fields`` stamps only those filled fields and keeps the other
        fields' sources. ``expected`` is what the person saw (``shown_value``); ConflictError
        (nothing written) when a stored value differs, as after enrichment wrote in between.
        Confirming twice writes nothing. NotFoundError (nothing written) for an unknown
        product."""
        with self._uow_factory() as uow:
            product = uow.products.get(product_id)
            if product is None:
                msg = f"product {product_id} does not exist"
                raise NotFoundError(msg)
            _refuse_changed(product, expected)
            filled = {
                "canonical_name": True,
                "brand": product.brand is not None,
                "size": product.size_value is not None,
                "category": product.category is not None,
                "tags": bool(product.tags),
            }
            stamped = {
                key: FieldSource.PERSON
                for key, value in filled.items()
                if value and (fields is None or key in fields)
            }
            sources = stamped if fields is None else {**product.field_sources, **stamped}
            if sources == product.field_sources:
                return product
            stored = uow.products.update(replace(product, field_sources=sources))
            uow.commit()
        log.info("catalog.confirmed", product_id=product_id)
        return stored


@dataclass(frozen=True, slots=True)
class ProductEdit:
    """What a person typed over a product's fields; None leaves a field as it is. ``size`` is
    text for ``parse_size``; an empty ``brand`` or ``size`` and no ``tags`` clear the field."""

    name: str | None = None
    brand: str | None = None
    size: str | None = None
    category: Category | None = None
    tags: tuple[str, ...] | None = None


class EditProduct:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(
        self,
        product_id: int,
        edit: ProductEdit,
        expected: Mapping[FieldSourceKey, ShownValue] | None = None,
    ) -> Product:
        """Write every given field and stamp it ``person`` (a cleared field is unstamped, like
        an empty field ``ConfirmProduct`` leaves), in one unit of work, and return the product
        as stored. Free text is masked and trimmed; tags are lowercased, blanks and repeats
        dropped. Nothing is written when nothing changes. ``expected`` is what the person saw,
        as for ``ConfirmProduct``: ConflictError (nothing written) when a stored value
        differs. DomainValidationError (nothing written) for a blank name, a tag with a comma
        or an unreadable size; NotFoundError for an unknown product."""
        if edit.name is not None and not edit.name.strip():
            msg = "a product's name is not blank"
            raise DomainValidationError(msg)
        if any("," in tag for tag in edit.tags or ()):
            msg = "a tag has no comma; give each tag on its own"
            raise DomainValidationError(msg)
        size = None if edit.size is None or not edit.size.strip() else parse_size(edit.size)
        with self._uow_factory() as uow:
            product = uow.products.get(product_id)
            if product is None:
                msg = f"product {product_id} does not exist"
                raise NotFoundError(msg)
            _refuse_changed(product, expected)
            edited = _edited(product, edit, size)
            if edited == product:
                return product
            stored = uow.products.update(edited)
            uow.commit()
        log.info("catalog.edited", product_id=product_id)
        return stored


def _edited(product: Product, edit: ProductEdit, size: tuple[Decimal, Unit] | None) -> Product:
    """``product`` with the person's fields written and stamped (unstamped when cleared)."""
    sources = dict(product.field_sources)

    def stamp(key: str, *, filled: bool) -> None:
        if filled:
            sources[key] = FieldSource.PERSON
        else:
            sources.pop(key, None)

    if edit.name is not None:
        product = replace(product, canonical_name=mask_pans(edit.name.strip()).text)
        stamp("canonical_name", filled=True)
    if edit.brand is not None:
        brand = mask_pans(edit.brand.strip()).text or None
        product = replace(product, brand=brand)
        stamp("brand", filled=brand is not None)
    if edit.size is not None:
        value, unit = (None, None) if size is None else size
        product = replace(product, size_value=value, size_unit=unit)
        stamp("size", filled=size is not None)
    if edit.category is not None:
        product = replace(product, category=edit.category)
        stamp("category", filled=True)
    if edit.tags is not None:
        tags = tuple(dict.fromkeys(mask_pans(tag.strip().lower()).text for tag in edit.tags))
        tags = tuple(tag for tag in tags if tag)
        product = replace(product, tags=tags)
        stamp("tags", filled=bool(tags))
    return replace(product, field_sources=sources)


@dataclass(frozen=True, slots=True)
class CodeNaming:
    """What ``catalog add`` was asked: name the product of ``code`` at ``store`` (``name`` with
    the optional fields), or make the code name an existing product (``product_id``, no
    fields). ``replace`` allows renaming a product a person already named."""

    store: str
    code: str
    name: str | None = None
    product_id: int | None = None
    brand: str | None = None
    size_value: Decimal | None = None
    size_unit: Unit | None = None
    category: Category | None = None
    replace: bool = False


@dataclass(frozen=True, slots=True)
class CodeNamed:
    """What naming a code does (``preview``) or did. ``before`` is the product the code named
    (None when the code was unknown), ``after`` that product or the new one as stored (``id``
    None in a preview of a new product). With ``product_id``, ``left`` is the code's previous
    product, kept with its ``left_lines`` ITEM lines (none is moved). ``changed`` is False when
    there is nothing to write."""

    code: CodeIdentity
    source: CodeSource
    before: Product | None
    after: Product
    left: Product | None
    left_lines: int
    changed: bool


class AddCatalogCode:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def preview(self, naming: CodeNaming) -> CodeNamed:
        """What ``__call__`` would do, from a read-only unit; writes nothing. Raises what
        ``__call__`` raises."""
        with self._uow_factory(read_only=True) as uow:
            return _code_naming(uow, naming, self._clock.now())

    def __call__(self, naming: CodeNaming) -> CodeNamed:
        """Name the code's product in one unit of work, reading it again inside the unit.

        The code is parsed with the store's profile (``product_code_at``). With ``name``: the
        product the code names is filled in place (a new product when it names none); the name
        and every given field are stamped ``person`` (free text masked) and the code is
        attached with source MANUAL. With ``product_id``: the code now names that product
        (source ADOPTED); its previous product and lines are left as they are. Nothing is
        written when nothing changes. DomainValidationError: an unparsable code, not exactly
        one of ``name`` and ``product_id``, or fields with ``product_id``. ConflictError
        naming the product (id and name) when it was named by a person, ``name`` differs and
        ``replace`` is false. NotFoundError for an unknown ``product_id``."""
        now = self._clock.now()
        with self._uow_factory() as uow:
            named = _code_naming(uow, naming, now)
            if not named.changed:
                return named
            product = named.after
            if named.before is None and product.id is None:
                product = uow.products.add(product)
            elif product != named.before and named.source is CodeSource.MANUAL:
                product = uow.products.update(product)
            attach_code(uow, named.code, require_id(product.id), named.source, now)
            uow.commit()
        log.info(
            "catalog.named",
            scheme=named.code.scheme.value,
            store_key=named.code.store_key,
            code=named.code.code,
            product_id=product.id,
            source=named.source.value,
        )
        return replace(named, after=product)


def _code_naming(uow: UnitOfWork, naming: CodeNaming, now: datetime) -> CodeNamed:
    """What naming the code does, read through ``uow`` (nothing written)."""
    if (naming.name is None) == (naming.product_id is None):
        msg = "give exactly one of a name and a product id"
        raise DomainValidationError(msg)
    if naming.name is not None and not naming.name.strip():
        msg = "a product's name is not blank"
        raise DomainValidationError(msg)
    fields = (naming.brand, naming.size_value, naming.category)
    if naming.product_id is not None and any(value is not None for value in fields):
        msg = "a brand, size or category is given with a name only"
        raise DomainValidationError(msg)
    if (naming.size_value is None) != (naming.size_unit is None):
        msg = "a size has a value and a unit"
        raise DomainValidationError(msg)
    store_key = normalize_store_name(naming.store)
    parsed = product_code_at(store_key, naming.code)
    identity = _required(parsed.identity_at(store_key))
    stored = stored_code(uow, parsed, identity)
    if stored is not None:
        identity = stored.identity
    before = None if stored is None else uow.products.get(stored.product_id)
    if naming.product_id is not None:
        target = uow.products.get(naming.product_id)
        if target is None:
            msg = f"product {naming.product_id} does not exist"
            raise NotFoundError(msg)
        left = before if before is not None and before.id != target.id else None
        return CodeNamed(
            code=identity,
            source=CodeSource.ADOPTED,
            before=before,
            after=target,
            left=left,
            left_lines=0 if left is None else len(uow.receipts.item_lines(require_id(left.id))),
            changed=stored is None
            or stored.product_id != target.id
            or stored.source is not CodeSource.ADOPTED,
        )
    name = mask_pans(str(naming.name)).text
    if (
        before is not None
        and before.field_sources.get("canonical_name") is FieldSource.PERSON
        and before.canonical_name != name
        and not naming.replace
    ):
        msg = f'product {before.id} "{before.canonical_name}" was named by a person'
        raise ConflictError(msg)
    after = _named(before, naming, name, now)
    return CodeNamed(
        code=identity,
        source=CodeSource.MANUAL,
        before=before,
        after=after,
        left=None,
        left_lines=0,
        changed=after != before or stored is None or stored.source is not CodeSource.MANUAL,
    )


def _named(before: Product | None, naming: CodeNaming, name: str, now: datetime) -> Product:
    """``before`` (or a new product) with the person's name and given fields, each ``person``."""
    product = before or Product(
        id=None,
        canonical_name=name,
        brand=None,
        size_value=None,
        size_unit=None,
        category=None,
        tags=(),
        created_at=now,
    )
    sources = {**product.field_sources, "canonical_name": FieldSource.PERSON}
    product = replace(product, canonical_name=name)
    if naming.brand is not None:
        product = replace(product, brand=mask_pans(naming.brand).text)
        sources["brand"] = FieldSource.PERSON
    if naming.size_value is not None:
        product = replace(product, size_value=naming.size_value, size_unit=naming.size_unit)
        sources["size"] = FieldSource.PERSON
    if naming.category is not None:
        product = replace(product, category=naming.category)
        sources["category"] = FieldSource.PERSON
    return replace(product, field_sources=sources)


def _required(identity: CodeIdentity | None) -> CodeIdentity:
    """The identity of a code already checked to have one (``product_code_at``, a coded line)."""
    if identity is None:
        msg = "expected a code with an identity"
        raise ValueError(msg)
    return identity


@dataclass(frozen=True, slots=True)
class CatalogCodeRow:
    """A stored code and the name of the product it names."""

    code: ProductCode
    product_name: str


class ListCatalogCodes:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(
        self, *, store: str | None = None, scheme: CodeScheme | None = None
    ) -> tuple[CatalogCodeRow, ...]:
        """Every stored code, ``store`` (normalized; a barcode or PLU is at store ``""``, so
        it is not listed under a store) and ``scheme`` filtering, ordered by scheme, store,
        code. Read-only."""
        store_key = None if store is None else normalize_store_name(store)
        with self._uow_factory(read_only=True) as uow:
            rows = [
                CatalogCodeRow(code, product.canonical_name)
                for code in uow.product_codes.list(store_key=store_key, scheme=scheme)
                if (product := uow.products.get(code.product_id)) is not None
            ]
        rows.sort(key=lambda row: (row.code.scheme.value, row.code.store_key, row.code.code))
        return tuple(rows)


class RemoveCatalogCode:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(self, store: str, code: str) -> ProductCode:
        """Delete the mapping of ``code`` read at ``store`` (``product_code_at``; the first of its
        identities that is stored) and return it; its product and lines are kept.
        DomainValidationError for an unparsable code, NotFoundError when no mapping exists."""
        store_key = normalize_store_name(store)
        parsed = product_code_at(store_key, code)
        identity = _required(parsed.identity_at(store_key))
        with self._uow_factory() as uow:
            stored = stored_code(uow, parsed, identity)
            if stored is None:
                msg = f"no product code {identity}"
                raise NotFoundError(msg)
            uow.product_codes.remove(stored.scheme, stored.store_key, stored.code)
            uow.commit()
        log.info(
            "catalog.removed",
            scheme=stored.scheme.value,
            store_key=stored.store_key,
            code=stored.code,
            product_id=stored.product_id,
        )
        return stored


@dataclass(frozen=True, slots=True)
class CodeLookup:
    """How ``catalog lookup`` reads a line: its code (None: the line starts with no code), the
    identity it is stored under (None for a code that names no product) and the first catalog
    entry for it (a ``store_catalog`` entry carries our product)."""

    store_key: str
    code: ItemCode | None
    identity: CodeIdentity | None
    entry: CatalogProduct | None


class LookupCatalogCode:
    def __init__(self, catalog: ProductCatalog) -> None:
        self._catalog = catalog

    def __call__(self, raw_text: str, store: str) -> CodeLookup:
        """Parse the line's code at ``store`` (normalized; a bare code is read as ``"<code> X"``,
        ``parse_bare_code``) and ask the catalog; a line without a code, or with one that names
        no product, is not looked up. Writes nothing."""
        store_key = normalize_store_name(store)
        code = parse_bare_code(raw_text.strip(), store_key) or parse_item_code(raw_text, store_key)
        identity = None if code is None else code.identity_at(store_key)
        entry = None
        if code is not None and identity is not None:
            entry = self._catalog.lookup(code, store_key)
        return CodeLookup(store_key, code, identity, entry)


class PluSkip(StrEnum):
    """Why a record of an IFPS export is not imported, in the summary's order."""

    RETAILER_ASSIGNED = "retailer assigned"
    DUPLICATE = "duplicate"
    INVALID_PLU = "invalid plu"
    NO_COMMODITY = "no commodity"


@dataclass(frozen=True, slots=True)
class PluImport:
    """What ``import_plu`` wrote: ``imported`` rows to ``path``, and the rows skipped by reason
    (only reasons that occurred, in ``PluSkip`` order)."""

    path: Path
    imported: int
    skipped: Mapping[PluSkip, int]


def plu_name(commodity: str, variety: str, size: str) -> str:
    """``"<Commodity>[, <Variety>][, <size>]"``: the commodity ``str.title()``-cased,
    the variety as printed (left out when empty or exactly ``Retailer Assigned``, ignoring
    case), the size lower-cased (left out when empty or ``All Sizes``, ignoring case).
    Cells are trimmed first."""
    variety, size = variety.strip(), size.strip()
    parts = [commodity.strip().title()]
    if variety and variety.casefold() != RETAILER_ASSIGNED_VARIETY.casefold():
        parts.append(variety)
    if size and size.casefold() != ALL_SIZES.casefold():
        parts.append(size.lower())
    return ", ".join(parts)


def import_plu(source: Path, target: Path) -> PluImport:
    """Write ``target`` (``code,name,organic``, sorted by code, ``organic`` always 0, names
    CSV-quoted when they hold a comma) from the IFPS export ``source``, read as ``utf-8-sig``
    columns by header (trimmed, any case: ``Plu``, ``Commodity``, ``Variety``,
    ``Size``, and ``Category`` when present), every cell trimmed. One reason per record,
    checked in this order: its category is ``Retailer Assigned Numbers`` (only when the
    column exists), its PLU is not four digits in 3000-4999 (invalid plu), its commodity is
    empty or missing (no commodity), its PLU came before (duplicate: the first importable
    record wins). The name is ``plu_name``. ``target``'s directory is created; the file is
    replaced atomically (a temporary file beside it, then a rename), so a failure leaves the
    old file and no other. DomainValidationError naming the column when one of the four is
    missing; NotFoundError ``nothing to import`` when no record is importable and
    ``{source} does not exist`` for a missing file; OSError/UnicodeDecodeError propagate."""
    if not source.exists():
        msg = f"{source} does not exist"
        raise NotFoundError(msg)
    names: dict[str, str] = {}
    skipped: Counter[PluSkip] = Counter()
    with source.open(encoding="utf-8-sig", newline="") as file:
        records = csv.reader(file)
        columns = _plu_columns(source, next(records, []))
        retailer = RETAILER_ASSIGNED_CATEGORY.casefold()
        for record in records:
            if not record:
                continue

            def cell(column: str, record: list[str] = record) -> str:
                at = columns.get(column)
                return record[at].strip() if at is not None and at < len(record) else ""

            code = cell(PLU_EXPORT_CODE)
            if PLU_EXPORT_CATEGORY in columns and cell(PLU_EXPORT_CATEGORY).casefold() == retailer:
                skipped[PluSkip.RETAILER_ASSIGNED] += 1
            elif not (_PLU.fullmatch(code) and int(code) in PLU_RANGE):
                skipped[PluSkip.INVALID_PLU] += 1
            elif not cell(PLU_EXPORT_COMMODITY):
                skipped[PluSkip.NO_COMMODITY] += 1
            elif code in names:
                skipped[PluSkip.DUPLICATE] += 1
            else:
                names[code] = plu_name(
                    cell(PLU_EXPORT_COMMODITY), cell(PLU_EXPORT_VARIETY), cell(PLU_EXPORT_SIZE)
                )
    if not names:
        msg = "nothing to import"
        raise NotFoundError(msg)
    _replace_file(target, [PLU_COLUMNS, *((code, names[code], "0") for code in sorted(names))])
    return PluImport(
        path=target,
        imported=len(names),
        skipped={reason: skipped[reason] for reason in PluSkip if skipped[reason]},
    )


def _plu_columns(source: Path, header: list[str]) -> dict[str, int]:
    """Each column's position by its trimmed, case-folded header (the first one wins)."""
    columns: dict[str, int] = {}
    for at, name in enumerate(header):
        columns.setdefault(name.strip().casefold(), at)
    for required in (PLU_EXPORT_CODE, PLU_EXPORT_COMMODITY, PLU_EXPORT_VARIETY, PLU_EXPORT_SIZE):
        if required not in columns:
            msg = f"{source} has no {required.title()} column"
            raise DomainValidationError(msg)
    return columns


def _replace_file(target: Path, rows: Iterable[Sequence[str]]) -> None:
    """Write ``rows`` as CSV to a temporary file beside ``target``, then rename it over it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        Path(temporary).chmod(0o644)  # mkstemp makes it 0600; the table is readable like the DB
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as file:
            csv.writer(file, lineterminator="\n").writerows(rows)
        Path(temporary).replace(target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
