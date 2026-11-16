"""Describing products from catalogs, never from a model (barcode-lookup plan 4.2, ADR-0018),
and the ``catalog`` commands (plan section 5).

``describe_product`` is what the supervisor's ``describe_from_catalog`` tool writes: only the
fields that come with a source, never over a person's, and a person's category for the line at
that store over any source's. It reads the product again inside its unit of work, so a person's
write between the caller's read and this one is kept.

The commands: ``CatalogTodo`` lists the coded products nobody has named yet, ``AddCatalogCode``
lets a person name one (or point its code at an existing product), ``ListCatalogCodes``,
``RemoveCatalogCode`` and ``LookupCatalogCode`` show and fix ``product_codes``, ``import_plu``
writes ``plu.csv`` from an IFPS export, and ``BackfillCatalog`` turns the products stored before
codes (``legacy_model``) into coded, printed-text ones. None of them calls a model or opens
a network connection.
"""

import csv
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from functools import partial
from pathlib import Path

import structlog

from paper_trail.application.matching import (
    PERSON_ALIASES,
    attach_code,
    latest_correction,
    stored_code,
)
from paper_trail.application.payloads import (
    CatalogEntryPayload,
    FieldSourceKey,
    ProductFieldsPayload,
    catalog_fields,
)
from paper_trail.application.products import new_product
from paper_trail.domain.enums import (
    AliasSource,
    Category,
    CodeScheme,
    CodeSource,
    CorrectionKind,
    FieldSource,
    LineKind,
    MatchSource,
    Unit,
)
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import (
    PERSON_CODE_SOURCES,
    CodeIdentity,
    Correction,
    ItemLink,
    Product,
    ProductAlias,
    ProductCode,
    ProductLine,
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
    printed_name,
)
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.domain.rules.normalization import (
    legacy_alias,
    normalize_alias,
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
_ALIAS_CORRECTIONS = frozenset({CorrectionKind.PRODUCT_LINK, CorrectionKind.CATEGORY})


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
    it as stored. A field whose stored source is ``person`` is kept (per field). The newest
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
    """``product`` with every sourced field written, except where a person's value stands."""
    written = dict(product.field_sources)
    described = product

    def open_for(key: FieldSourceKey) -> bool:
        if key not in sources or written.get(key) is FieldSource.PERSON:
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
    """One code of a product nobody has named yet, and how it was bought (at the asked store
    only, when one was asked). ``last_paid``/``last_date`` are the newest price observation's
    paid price and date (None when its lines have none). ``lookup_url`` is the store's search
    link for the code, never requested here."""

    code: CodeIdentity
    product_id: int
    printed_name: str
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
        """Every code of an unconfirmed product (``is_confirmed`` false) that has ITEM lines,
        one row per code: most lines first, then the newest ``last_date``, then the code.
        ``store`` (normalized with ``normalize_store_name``) keeps the codes bought there and
        counts only those lines; ``limit`` keeps the first rows. Read-only."""
        store_key = None if store is None else normalize_store_name(store)
        rows: list[TodoRow] = []
        with self._uow_factory(read_only=True) as uow:
            for code in uow.product_codes.list():
                product = uow.products.get(code.product_id)
                if product is None or is_confirmed(product):
                    continue
                lines = [
                    line
                    for line in uow.receipts.item_lines(code.product_id)
                    if store_key is None or line.store_key == store_key
                ]
                if not lines:
                    continue
                history = [
                    observation
                    for observation in uow.prices.history(code.product_id)
                    if store_key is None or observation.store_key == store_key
                ]
                newest = history[-1] if history else None
                rows.append(
                    TodoRow(
                        code=code.identity,
                        product_id=code.product_id,
                        printed_name=product.canonical_name,
                        times_bought=len(lines),
                        last_paid=None if newest is None else newest.paid,
                        last_date=None if newest is None else newest.observed_on,
                        lookup_url=lookup_url(code.store_key, code.code),
                    )
                )
        rows.sort(
            key=lambda row: (
                -row.times_bought,
                row.last_date is None,
                -(row.last_date or date.min).toordinal(),
                row.code.code,
            )
        )
        return tuple(rows if limit is None else rows[:limit])


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


@dataclass(frozen=True, slots=True)
class BackfilledProduct:
    """A product the backfill changes (or would): the code it keeps (None: uncoded lines
    only), its ITEM lines after the splits, its fields and sources before and after, and
    whether its code row is written."""

    product_id: int
    code: CodeIdentity | None
    lines: int
    before: Product
    after: Product
    code_attached: bool


@dataclass(frozen=True, slots=True)
class BackfillSplit:
    """A line moved to the product of its own code. ``to_product_id`` is None when the
    backfill creates that product (in a dry run and when applying alike); ``to_name`` is its
    name."""

    receipt_id: int
    position: int
    code: CodeIdentity
    from_product_id: int
    to_product_id: int | None
    to_name: str


class PersonLink(StrEnum):
    """Why a line is a person's link and never moves."""

    CORRECTION = "correction"  # match_source memory
    IMPORT_ALIAS = "import alias"  # an IMPORT alias at its store for its text names its product


@dataclass(frozen=True, slots=True)
class BackfillConflict:
    """A line whose code belongs elsewhere but that a person linked: it stays, and its code is
    not attached to its product."""

    receipt_id: int
    position: int
    product_id: int
    code: CodeIdentity
    reason: PersonLink


@dataclass(frozen=True, slots=True)
class MergeCandidate:
    """Two products whose lines carry the same code: ``product_id`` holds the code, the other
    keeps its lines and gets none. Never merged."""

    code: CodeIdentity
    product_id: int
    other_product_id: int


@dataclass(frozen=True, slots=True)
class CatalogBackfill:
    """What the backfill did (``applied``) or would do. ``products`` in id order, ``splits``
    and ``conflicts`` by receipt then position, ``merge_candidates`` by code."""

    products: tuple[BackfilledProduct, ...]
    splits: tuple[BackfillSplit, ...]
    conflicts: tuple[BackfillConflict, ...]
    merge_candidates: tuple[MergeCandidate, ...]
    codes_learned: int
    aliases_renormalized: int
    corrections_renormalized: int
    applied: bool

    @property
    def changes(self) -> int:
        """Written rows' worth of changes: 0 on a second ``--apply``."""
        return (
            len(self.products)
            + len(self.splits)
            + self.codes_learned
            + self.aliases_renormalized
            + self.corrections_renormalized
        )


class CatalogBackfillStoppedError(Exception):
    """A unit of work failed (``step``: ``dry run``, ``codes``, ``products``, ``category
    corrections``, ``product N``, ``corrections`` or ``aliases``); when applying, the steps
    and products before it stay applied."""

    def __init__(self, step: str, error: Exception) -> None:
        super().__init__(f"{type(error).__name__}: {error}")
        self.step = step


type _CategoryIndex = Mapping[tuple[str, str | None], tuple[Correction, ...]]


@dataclass(frozen=True, slots=True)
class _Owners:
    """Who owns each code for the whole run, read once before any product unit and never
    recomputed. ``main`` is each product's most frequent code it holds no row for, among the
    lines no person linked to it (tie: the earliest such line); ``owner`` the product a code
    ends up on: the one holding its row, else the product with the most lines of it among
    those whose main code it is (tie: the lowest id), else None for a product this run
    creates; ``first`` the earliest line carrying a code that its product may give up, which
    is the earliest line of a created product's final lines and so names it."""

    main: Mapping[int, CodeIdentity]
    owner: Mapping[CodeIdentity, int | None]
    first: Mapping[CodeIdentity, ProductLine]


@dataclass
class _Found:
    """What the product steps found so far (a product visited again replaces its own rows),
    what they read (the CATEGORY corrections, the owner of each code) and the products this
    run created, each with its code."""

    categories: _CategoryIndex
    owners: _Owners
    products: dict[int, BackfilledProduct] = field(default_factory=dict)
    splits: list[BackfillSplit] = field(default_factory=list)
    conflicts: dict[tuple[int, int], BackfillConflict] = field(default_factory=dict)
    merge_candidates: dict[MergeCandidate, None] = field(default_factory=dict)
    created: dict[int, CodeIdentity] = field(default_factory=dict)
    originals: dict[int, Product] = field(default_factory=dict)
    attached: set[int] = field(default_factory=set)
    reshaped: set[int] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _Coded:
    """A product's ITEM line and its code read at the line's own store."""

    line: ProductLine
    code: ItemCode | None
    identity: CodeIdentity | None


type _Units = Callable[[], AbstractContextManager[UnitOfWork]]


class BackfillCatalog:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock, plu: ProductCatalog) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._plu = plu

    def __call__(self, *, apply: bool = False) -> CatalogBackfill:
        """Give every product stored before codes its code and printed-text identity (plan
        section 5.5). A dry run by default; ``apply`` writes. First it learns codes from
        PRODUCT_LINK corrections (newest per code, source ``correction``). Then per product, in
        one unit of work with every product its split touches: splits off lines of other codes
        (person-linked lines stay, reported), attaches its most frequent code (tie: the
        earliest line's; source ``backfill``), resets ``legacy_model`` fields to the printed
        text, keeps a person's name and category (stamped ``person``), fills from the PLU
        table what a person did not keep and re-points the MATCHED aliases of moved lines. A
        person's aliases and corrections are read under both the old and the new
        normalization, so the last two steps can re-key correction subjects (in place) and
        aliases. Converges: a second ``apply`` reports 0 changes and writes nothing. No model.
        CatalogBackfillStoppedError names the failed step.

        The dry run runs the same steps in one writing unit that is never committed, so each
        step sees the ones before it and the report equals what ``apply`` does."""
        if apply:
            backfill = self._backfill(self._uow_factory, applied=True)
        else:
            try:
                dry_run = self._uow_factory()
            except Exception as error:
                raise CatalogBackfillStoppedError("dry run", error) from error
            with dry_run as uow:
                backfill = self._backfill(lambda: nullcontext(uow), applied=False)
        log.info(
            "catalog.backfilled",
            applied=apply,
            products=len(backfill.products),
            splits=len(backfill.splits),
            conflicts=len(backfill.conflicts),
            merge_candidates=len(backfill.merge_candidates),
            changes=backfill.changes,
        )
        return backfill

    def _backfill(self, units: _Units, *, applied: bool) -> CatalogBackfill:
        now = self._clock.now()
        learned = self._step(units, "codes", partial(_learn_codes, now=now), commit=applied)
        product_ids = self._step(units, "products", lambda uow: uow.products.ids(), commit=False)
        categories = self._step(units, "category corrections", _category_index, commit=False)
        owners = self._step(
            units, "owners", partial(_owners, product_ids=product_ids), commit=False
        )
        found = _Found(categories, owners)
        for product_id in product_ids:
            self._step(
                units,
                f"product {product_id}",
                partial(self._group, product_id=product_id, now=now, found=found),
                commit=applied,
            )
        corrections = self._step(units, "corrections", _rekey_corrections, commit=applied)
        aliases = self._step(units, "aliases", _renormalize_aliases, commit=applied)
        return CatalogBackfill(
            products=tuple(found.products[key] for key in sorted(found.products)),
            splits=tuple(sorted(found.splits, key=lambda row: (row.receipt_id, row.position))),
            conflicts=tuple(found.conflicts[key] for key in sorted(found.conflicts)),
            merge_candidates=tuple(
                sorted(
                    found.merge_candidates,
                    key=lambda row: (
                        row.code.scheme.value,
                        row.code.store_key,
                        row.code.code,
                        row.other_product_id,
                    ),
                )
            ),
            codes_learned=learned,
            aliases_renormalized=aliases,
            corrections_renormalized=corrections,
            applied=applied,
        )

    @staticmethod
    def _step[T](units: _Units, step: str, action: Callable[[UnitOfWork], T], *, commit: bool) -> T:
        """Run ``action`` in its own unit (committed when applying); a failure names the step
        and leaves nothing of it written."""
        try:
            with units() as uow:
                result = action(uow)
                if commit:
                    uow.commit()
        except Exception as error:
            raise CatalogBackfillStoppedError(step, error) from error
        return result

    def _group(self, uow: UnitOfWork, *, product_id: int, now: datetime, found: _Found) -> None:
        """One product and, in the same ``uow``, every product its split moves lines to (and
        theirs in turn). Once no more lines move, every product the group touched is settled
        from its final lines and reported, and the moves are reported with their names."""
        group: set[int] = set()
        moves: list[tuple[_Coded, int, int, CodeIdentity]] = []
        self._product(uow, product_id, now=now, found=found, group=group, moves=moves)
        found.reshaped.update(pid for _, source, target, _ in moves for pid in (source, target))
        for touched in sorted(group):
            self._finish(uow, touched, found, moved_from=any(m[1] == touched for m in moves))
        for member, source, target, code in moves:
            found.splits.append(
                BackfillSplit(
                    receipt_id=member.line.receipt_id,
                    position=member.line.line.position,
                    code=code,
                    from_product_id=source,
                    to_product_id=None if target in found.created else target,
                    to_name=_name_of(uow, target),
                )
            )

    def _product(  # noqa: PLR0913 - the group's unit, the product and the group's bookkeeping
        self,
        uow: UnitOfWork,
        product_id: int,
        *,
        now: datetime,
        found: _Found,
        group: set[int],
        moves: list[tuple[_Coded, int, int, CodeIdentity]],
    ) -> None:
        """Move the product's lines whose code another product owns (a person's links stay,
        reported), attach the codes it owns, then do the same for each product it moved lines
        to that the group has not done yet."""
        group.add(product_id)
        product = uow.products.get(product_id)
        lines = uow.receipts.item_lines(product_id)
        if product is None or not lines:
            return
        found.originals.setdefault(product_id, product)
        main = found.owners.main.get(product_id)
        staying: list[_Coded] = []
        moved: list[tuple[_Coded, int]] = []
        for member in (_coded(line) for line in lines):
            identity = member.identity
            if (
                identity is None
                or identity == main
                or _owns(found, identity, product_id)
                or _holds(uow, member, product_id)
            ):
                staying.append(member)
                continue
            reason = _person_link(uow, member, product_id)
            if reason is not None:
                staying.append(member)
                key = (member.line.receipt_id, member.line.line.position)
                found.conflicts[key] = BackfillConflict(*key, product_id, identity, reason)
                continue
            owner = found.owners.owner.get(identity)
            target = owner if owner is not None else self._created(uow, member, now, found)
            _move(uow, member, target)
            moved.append((member, target))
            moves.append((member, product_id, target, identity))
        _repoint_aliases(uow, product_id, moved, staying)
        owned = [m for m in staying if m.identity and _owns(found, m.identity, product_id)]
        if _attach_owned(uow, product_id, owned, now):
            found.attached.add(product_id)
        main_owner = None if main is None else found.owners.owner.get(main)
        if main is not None and main_owner is not None and main_owner != product_id:
            found.merge_candidates[MergeCandidate(main, main_owner, product_id)] = None
        for _, target in moved:
            if target in found.created:
                group.add(target)
            elif target not in group:
                self._product(uow, target, now=now, found=found, group=group, moves=moves)

    def _finish(self, uow: UnitOfWork, product_id: int, found: _Found, *, moved_from: bool) -> None:
        """Settle the product from the fields it had when this run first read it and its own
        final lines only, so a later settle in the run, a second run or a resumed one gives the
        same values; then report the product."""
        product = uow.products.get(product_id)
        if product is None:
            return
        original = found.originals.get(product_id, product)
        members = [_coded(line) for line in uow.receipts.item_lines(product_id)]
        if not members:
            return
        keeper = _main_line(members)
        settled = self._settled(
            uow, original, members, keeper, found.categories, renamed=product_id in found.reshaped
        )
        if settled != product:
            uow.products.update(settled)
        if product_id in found.created:
            return
        earlier = found.products.get(product_id)
        attached = product_id in found.attached
        unchanged = settled == original and not (attached or moved_from)
        if earlier is None and unchanged:
            return
        found.products[product_id] = BackfilledProduct(
            product_id=product_id,
            code=None if keeper is None else keeper.identity,
            lines=len(members),
            before=original,
            after=settled,
            code_attached=attached,
        )

    def _created(self, uow: UnitOfWork, member: _Coded, now: datetime, found: _Found) -> int:
        """The one product this run creates for a code no product owns, with its code row,
        named from the earliest of the lines that will move to it."""
        identity = _required(member.identity)
        for created, code in found.created.items():
            if code == identity:
                return created
        product = new_product(
            uow,
            printed_name(found.owners.first.get(identity, member.line).line.raw_text),
            None,
            now,
            field_sources={"canonical_name": FieldSource.RECEIPT_TEXT},
        )
        created = require_id(product.id)
        attach_code(uow, identity, created, CodeSource.BACKFILL, now)
        found.created[created] = identity
        found.originals[created] = product
        return created

    def _settled(  # noqa: PLR0913 - the product, its lines, naming line, corrections and flag
        self,
        uow: UnitOfWork,
        product: Product,
        staying: Sequence[_Coded],
        keeper: _Coded | None,
        categories: _CategoryIndex,
        *,
        renamed: bool,
    ) -> Product:
        """A person's values read first and kept as ``person``, ``legacy_model`` fields reset
        (the name to the printed text of ``keeper``, else of the first of ``staying``; with
        ``renamed``, lines moved in or out of it this run, a ``receipt_text`` name too), then the
        PLU table's fields where no person's stand. ``staying`` is every line whose
        CATEGORY corrections count for the product: a ``person`` category one of them gave
        yields to the newest of them; a category a person typed elsewhere stays."""
        sources = dict(product.field_sources)
        settled = product
        name_source = sources.get("canonical_name")
        if name_source is FieldSource.LEGACY_MODEL and _person_named(uow, product):
            sources["canonical_name"] = FieldSource.PERSON
        elif staying and (
            name_source is FieldSource.LEGACY_MODEL
            or (renamed and name_source is FieldSource.RECEIPT_TEXT)
        ):
            first = keeper or staying[0]
            settled = replace(settled, canonical_name=printed_name(first.line.line.raw_text))
            sources["canonical_name"] = FieldSource.RECEIPT_TEXT
        if sources.get("brand") is FieldSource.LEGACY_MODEL:
            settled = replace(settled, brand=None)
            del sources["brand"]
        if sources.get("size") is FieldSource.LEGACY_MODEL:
            settled = replace(settled, size_value=None, size_unit=None)
            del sources["size"]
        if sources.get("category") is FieldSource.LEGACY_MODEL:
            settled = replace(settled, category=None)
            del sources["category"]
        corrected = {
            row.new_value for rows in _corrections_for(categories, staying) for row in rows
        }
        if sources.get("category") is FieldSource.PERSON and settled.category in corrected:
            del sources["category"]
        if sources.get("tags") is FieldSource.LEGACY_MODEL:
            settled = replace(settled, tags=())
            del sources["tags"]
        settled = replace(settled, field_sources=sources)
        fields = ProductFieldsPayload()
        field_sources: dict[FieldSourceKey, FieldSource] = {}
        if keeper is not None and keeper.code is not None and keeper.code.scheme is CodeScheme.PLU:
            entry = self._plu.lookup(keeper.code, keeper.line.store_key)
            if entry is not None:
                fields, field_sources = catalog_fields(CatalogEntryPayload.of(entry))
        return _described(settled, fields, field_sources, _remembered_category(categories, staying))


def _coded(line: ProductLine) -> _Coded:
    code = parse_item_code(line.line.raw_text, line.store_key)
    return _Coded(line, code, None if code is None else code.identity_at(line.store_key))


def _name_of(uow: UnitOfWork, product_id: int) -> str:
    product = uow.products.get(product_id)
    return "" if product is None else product.canonical_name


def _main_line(members: Sequence[_Coded]) -> _Coded | None:
    """The earliest line of the most frequent code among the lines (tie: the earliest line)."""
    counts = Counter(member.identity for member in members if member.identity is not None)
    if not counts:
        return None
    most = max(counts.values())
    return next(member for member in members if member.identity and counts[member.identity] == most)


def _owns(found: _Found, identity: CodeIdentity, product_id: int) -> bool:
    return found.owners.owner.get(identity) == product_id


def _holds(uow: UnitOfWork, member: _Coded, product_id: int) -> bool:
    """The product holds the line's code row now (a person's write during the run)."""
    if member.code is None or member.identity is None:
        return False
    row = stored_code(uow, member.code, member.identity)
    return row is not None and row.product_id == product_id


def _owners(uow: UnitOfWork, *, product_ids: Sequence[int]) -> _Owners:
    """Each code's owner for the whole run, from the lines and code rows as they are now."""
    held: dict[CodeIdentity, int | None] = {}
    main: dict[int, CodeIdentity] = {}
    best: dict[CodeIdentity, tuple[int, int]] = {}
    first: dict[CodeIdentity, ProductLine] = {}
    for product_id in product_ids:
        coded = [_coded(line) for line in uow.receipts.item_lines(product_id)]
        for member in coded:
            if member.identity is None or member.code is None:
                continue
            if member.identity not in held:
                row = stored_code(uow, member.code, member.identity)
                held[member.identity] = None if row is None else row.product_id
        unheld = [
            member
            for member in coded
            if member.identity is not None
            and held[member.identity] != product_id
            and _person_link(uow, member, product_id) is None
        ]
        for member in unheld:
            known = first.get(_required(member.identity))
            if known is None or _earliest(member.line) < _earliest(known):
                first[_required(member.identity)] = member.line
        counts = Counter(member.identity for member in unheld)
        if not counts:
            continue
        lines = max(counts.values())
        code = _required(next(m.identity for m in unheld if counts[m.identity] == lines))
        main[product_id] = code
        current = best.get(code)
        if current is None or (-lines, product_id) < (-current[0], current[1]):
            best[code] = (lines, product_id)
    owner = {
        code: product_id if product_id is not None else best.get(code, (0, None))[1]
        for code, product_id in held.items()
    }
    return _Owners(main, owner, first)


def _earliest(line: ProductLine) -> tuple[bool, date, int, int]:
    return (
        line.purchased_on is None,
        line.purchased_on or date.min,
        line.receipt_id,
        line.line.position,
    )


def _person_link(uow: UnitOfWork, member: _Coded, product_id: int) -> PersonLink | None:
    """Why a person linked the line to the product, or None."""
    if member.line.line.match_source is MatchSource.MEMORY:
        return PersonLink.CORRECTION
    for key in _alias_keys(member):
        alias = uow.products.alias_at(key, member.line.store_key)
        if (
            alias is not None
            and alias.source is AliasSource.IMPORT
            and alias.product_id == product_id
        ):
            return PersonLink.IMPORT_ALIAS
    return None


def _move(uow: UnitOfWork, member: _Coded, target: int) -> None:
    """The line and its price observation now belong to ``target`` (discounts kept)."""
    line = member.line.line
    source = line.match_source or MatchSource.NEW
    uow.receipts.link_items(member.line.receipt_id, [ItemLink(line.position, target, source)])
    uow.prices.reassign(require_id(line.id), target)


def _repoint_aliases(
    uow: UnitOfWork,
    product_id: int,
    moved: Sequence[tuple[_Coded, int]],
    staying: Sequence[_Coded],
) -> None:
    """The MATCHED alias of a moved line's text at its store (under either normalization)
    follows the line; one that staying lines share is deleted. A person's alias is never
    touched."""
    kept = {(key, member.line.store_key) for member in staying for key in _alias_keys(member)}
    for member, target in moved:
        for key in _alias_keys(member):
            alias = uow.products.alias_at(key, member.line.store_key)
            if (
                alias is None
                or alias.source is not AliasSource.MATCHED
                or alias.product_id != product_id
            ):
                continue
            if (key, member.line.store_key) in kept:
                uow.products.remove_alias(require_id(alias.id))
            else:
                uow.products.update_alias(replace(alias, product_id=target))


def _alias_keys(member: _Coded) -> tuple[str, ...]:
    """A line's alias and the one it had before the tax-flag change (once when equal)."""
    text = member.line.line.raw_text
    return tuple(dict.fromkeys((normalize_alias(text), legacy_alias(text))))


def _attach_owned(uow: UnitOfWork, product_id: int, owned: Sequence[_Coded], now: datetime) -> bool:
    """Attach (machine policy) each code the product owns that has no row yet."""
    attached = False
    for member in owned:
        if member.code is None or member.identity is None:
            continue
        if stored_code(uow, member.code, member.identity) is None:
            attach_code(uow, member.identity, product_id, CodeSource.BACKFILL, now)
            attached = True
    return attached


def _person_named(uow: UnitOfWork, product: Product) -> bool:
    """A person created the product (a PRODUCT_LINK correction made with it) or named it in an
    import or a review answer (an IMPORT alias)."""
    product_id = require_id(product.id)
    if any(alias.source is AliasSource.IMPORT for alias in uow.products.aliases(product_id)):
        return True
    return any(
        correction.new_value == str(product_id) and correction.created_at == product.created_at
        for correction in uow.corrections.list(CorrectionKind.PRODUCT_LINK)
    )


def _category_index(uow: UnitOfWork) -> dict[tuple[str, str | None], tuple[Correction, ...]]:
    """The CATEGORY corrections per normalized subject and store, newest first, read from the
    rows as stored (before their subjects are re-keyed), so an old-key row counts under its new
    key."""
    index: dict[tuple[str, str | None], list[Correction]] = defaultdict(list)
    for correction in uow.corrections.list(CorrectionKind.CATEGORY):
        key = (normalize_alias(correction.subject) or correction.subject, correction.store_key)
        index[key].append(correction)
    return {
        key: tuple(sorted(rows, key=lambda row: (row.created_at, require_id(row.id)), reverse=True))
        for key, rows in index.items()
    }


def _remembered_category(categories: _CategoryIndex, staying: Sequence[_Coded]) -> Category | None:
    """The newest CATEGORY correction for the texts of the product's lines at their stores."""
    found = [rows[0] for rows in _corrections_for(categories, staying)]
    newest = max(found, key=lambda row: (row.created_at, require_id(row.id)), default=None)
    if newest is None or newest.new_value not in Category:
        return None
    return Category(newest.new_value)


def _corrections_for(
    categories: _CategoryIndex, staying: Sequence[_Coded]
) -> list[tuple[Correction, ...]]:
    keys = dict.fromkeys(
        (normalize_alias(member.line.line.raw_text), member.line.store_key) for member in staying
    )
    return [categories[key] for key in keys if key in categories]


def _renormalize_aliases(uow: UnitOfWork) -> int:
    """Every alias under ``normalize_alias`` of its text, after the products are settled (they
    read a person's aliases under both keys first). On a collision at one store a person's
    alias beats a machine's, then the newest; the others are deleted. The lines a deleted
    IMPORT alias protected become ``memory`` links in the same unit, so they stay a person's.
    How many rows were rewritten or removed."""
    groups: dict[tuple[str, str | None], list[ProductAlias]] = defaultdict(list)
    for alias in uow.products.all_aliases():
        key = normalize_alias(alias.alias) or alias.alias
        groups[key, alias.store_key].append(alias)
    changed = 0
    for (key, _), rows in groups.items():
        if all(row.alias == key for row in rows):
            continue
        winner = max(
            rows, key=lambda row: (row.source in PERSON_ALIASES, row.created_at, require_id(row.id))
        )
        for row in rows:
            if row is not winner:
                if row.source is AliasSource.IMPORT:
                    _remember_links(uow, row)
                uow.products.remove_alias(require_id(row.id))
                changed += 1
        if winner.alias != key:
            uow.products.update_alias(replace(winner, alias=key))
            changed += 1
    return changed


def _remember_links(uow: UnitOfWork, alias: ProductAlias) -> None:
    """The lines ``alias`` named (its product's, at its store, under either normalization)
    are linked as ``memory``."""
    by_receipt: dict[int, list[ItemLink]] = defaultdict(list)
    for found in uow.receipts.item_lines(alias.product_id):
        line = found.line
        if (
            line.match_source is not MatchSource.MEMORY
            and alias.store_key in {None, found.store_key}
            and alias.alias in {normalize_alias(line.raw_text), legacy_alias(line.raw_text)}
        ):
            by_receipt[found.receipt_id].append(
                ItemLink(line.position, alias.product_id, MatchSource.MEMORY)
            )
    for receipt_id, links in by_receipt.items():
        uow.receipts.link_items(receipt_id, links)


def _rekey_corrections(uow: UnitOfWork) -> int:
    """Every PRODUCT_LINK and CATEGORY correction's subject under ``normalize_alias``, in
    place: none is deleted (the newest one for a key still wins a lookup). How many were
    re-keyed."""
    changed = 0
    for correction in uow.corrections.list():
        key = normalize_alias(correction.subject) or correction.subject
        if correction.kind in _ALIAS_CORRECTIONS and correction.subject != key:
            uow.corrections.update(replace(correction, subject=key))
            changed += 1
    return changed


def _learn_codes(uow: UnitOfWork, now: datetime) -> int:
    """Each code read from the line a PRODUCT_LINK correction was made on names that
    correction's product, the newest correction per code winning (source ``correction``). It
    fills a missing row or replaces a machine's, never a person's. How many rows were
    written."""
    newest: dict[CodeIdentity, tuple[Correction, ItemCode]] = {}
    for correction in uow.corrections.list(CorrectionKind.PRODUCT_LINK):
        read = _corrected_code(uow, correction)
        if read is None:
            continue
        identity, code = read
        current = newest.get(identity)
        if current is None or (correction.created_at, require_id(correction.id)) > (
            current[0].created_at,
            require_id(current[0].id),
        ):
            newest[identity] = (correction, code)
    learned = 0
    for identity, (correction, code) in newest.items():
        target = int(correction.new_value)
        stored = stored_code(uow, code, identity)
        if stored is not None and stored.source in PERSON_CODE_SOURCES:
            continue
        attach_code(
            uow,
            identity if stored is None else stored.identity,
            target,
            CodeSource.CORRECTION,
            now,
        )
        learned += 1
    return learned


def _corrected_code(
    uow: UnitOfWork, correction: Correction
) -> tuple[CodeIdentity, ItemCode] | None:
    """The code of the line a PRODUCT_LINK correction was made on, read at its receipt's
    store, when its target product still exists."""
    if correction.receipt_id is None or correction.line_item_id is None:
        return None
    if not correction.new_value.isdigit() or uow.products.get(int(correction.new_value)) is None:
        return None
    receipt = uow.receipts.get(correction.receipt_id)
    line = (
        None
        if receipt is None
        else next((item for item in receipt.items if item.id == correction.line_item_id), None)
    )
    if receipt is None or line is None or line.kind is not LineKind.ITEM:
        return None
    code = parse_item_code(line.raw_text, receipt.store_key)
    identity = None if code is None else code.identity_at(receipt.store_key)
    return None if code is None or identity is None else (identity, code)
