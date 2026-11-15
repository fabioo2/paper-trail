"""``BackfillCatalog``: products stored before codes become coded, printed-text products
(barcode-lookup plan R18, A3, A4, A7, A8, Q4; PC-TC-125..137, 139 (unit half), 142, 158, 171).

Pinned (signatures "PR 1b", rulings I6, I7, I10, I11, I13, TQ19, TQ35):
- ``BackfillCatalog(uow_factory, clock, plu)(*, apply=False) -> CatalogBackfill``; a dry run
  writes nothing and reports exactly what ``apply`` does (a product the run creates is
  ``to_product_id=None`` in both modes).
- One owner per code for the whole run (I23), decided after code learning and before any
  product unit, never recomputed: ``R(c)`` the product holding ``c``'s row; ``M(p)`` the most
  frequent code among ``p``'s lines whose code ``p`` holds no row for (tie: the earliest line,
  by receipt date then position); ``O(c)`` = ``R(c)``, else the ``M``-holder with the most
  ``c`` lines (tie: lowest id), else one new printed-text product for ``c`` shared by every
  product that sends it lines (named from the earliest ``c`` line). A line stays when
  ``O(c) = p`` or ``c = M(p)``, else moves to ``O(c)``; ``p`` attaches ``c`` (``backfill``)
  when ``O(c) = p`` and no row exists; an ``M``-holder that is not the owner keeps its lines,
  attaches nothing and is a merge candidate. A product may own several codes. Person-linked
  lines (``memory``, or an IMPORT alias at its store naming the product) never move
  (conflicts). The outcome is the same in every id order, after a second apply, and after a
  crash between units and a resume.
- ``legacy_model`` fields are reset to the printed text of the earliest line of its main code
  (else the earliest line); a person's name
  (creating PRODUCT_LINK correction, IMPORT alias) and newest CATEGORY correction are kept as
  ``person``; ``person``/``plu``/``receipt_text`` fields are left; a PLU code fills what no
  person kept. Unconfirmed products lose their vector. MATCHED aliases of moved lines follow
  them (deleted when staying lines share the text); person aliases are untouched.
- PRODUCT_LINK corrections teach codes first, from their original subjects and lines (newest
  per code, source ``correction``), never over a person's code row; person evidence (IMPORT
  aliases under either key) is read before stored aliases are re-normalized; correction
  subjects are re-keyed in place and never deleted (A8).
- A product the run creates is described from every line moved into it, so a second ``apply``
  reports 0 changes and writes nothing.

The legacy state (``Legacy``): products as 0006 stamps them with a vector each, receipts whose
linked ITEM lines have an observation (discounts applied) and a MATCHED alias per distinct line
text at its store, stored under the current ``normalize_alias`` key.
"""

import copy
import itertools
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import date, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from paper_trail.adapters.catalog.plu import PluCatalog
from paper_trail.application.catalog import (
    BackfillCatalog,
    BackfillConflict,
    BackfilledProduct,
    BackfillSplit,
    CatalogBackfill,
    CatalogBackfillStoppedError,
    MergeCandidate,
    PersonLink,
)
from paper_trail.application.matching import find_product, latest_correction
from paper_trail.domain.enums import (
    AliasSource,
    Category,
    CodeScheme,
    CodeSource,
    CorrectionKind,
    FieldSource,
    LineKind,
    MatchSource,
)
from paper_trail.domain.models import (
    CodeIdentity,
    Correction,
    LineItem,
    Product,
    ProductAlias,
    ProductCode,
    Receipt,
)
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.agent_harness import build_harness
from tests.extractions import (
    C1,
    C2,
    L1,
    L3,
    L4,
    LOBLAWS,
    NOW,
    PLU_CSV,
    U1,
    WALMART,
    coded_extraction,
)
from tests.factories import (
    CREATED_AT,
    STORE_KEY,
    STORE_NAME,
    at,
    correction,
    discount_line,
    item_line,
    legacy_product,
    line_item,
    product,
    product_alias,
)
from tests.fakes.clock import FakeClock
from tests.fakes.llm import ScriptedLlm
from tests.fakes.repositories import FakeReceiptRepository
from tests.fakes.unit_of_work import FakeDatabase, FakeUnitOfWork, fake_uow_factory
from tests.storage import in_unit, seed_purchase
from tests.support import require_id

X = CodeIdentity(CodeScheme.STORE, STORE_KEY, "1362394")
Y = CodeIdentity(CodeScheme.STORE, STORE_KEY, "1543210")
WIPES = CodeIdentity(CodeScheme.GTIN, "", "0060383064877")
APPLE = CodeIdentity(CodeScheme.PLU, "", "3101")
ORGANIC_APPLE = CodeIdentity(CodeScheme.PLU, "", "93101")
COSTCO_APPLE = CodeIdentity(CodeScheme.STORE, STORE_KEY, "3101")
FIRST_DAY = date(2026, 11, 1)
FIELD_KEYS = ("canonical_name", "brand", "size", "category", "tags")
PRINTED = {"canonical_name": FieldSource.RECEIPT_TEXT}

type CodeRow = tuple[int, CodeScheme, str, str, CodeSource]


class Legacy:
    """Arranges stored data through the unit of work, one committed unit per call."""

    def __init__(self, factory: UnitOfWorkFactory) -> None:
        self.factory = factory
        self._days = count()

    def add(self, stored: Product, *, vector: bool = True) -> int:
        def store(uow: UnitOfWork) -> int:
            product_id = require_id(uow.products.add(stored).id)
            if vector:
                uow.vectors.upsert(product_id, [1.0] + [0.0] * (uow.vectors.dimension - 1))
            return product_id

        return in_unit(self.factory, store)

    def buy(
        self,
        *lines: LineItem,
        store: str = STORE_NAME,
        on: date | None = None,
        alias_source: AliasSource = AliasSource.MATCHED,
    ) -> Receipt:
        day = FIRST_DAY + timedelta(days=next(self._days))
        stored = seed_purchase(self.factory, lines, store_name=store, purchased_on=on or day)

        def remember(uow: UnitOfWork) -> None:
            for item in stored.items:
                key = normalize_alias(item.raw_text)
                if (
                    item.kind is LineKind.ITEM
                    and item.product_id is not None
                    and uow.products.alias_at(key, stored.store_key) is None
                ):
                    uow.products.add_alias(
                        product_alias(
                            item.product_id,
                            alias=key,
                            store_key=stored.store_key,
                            source=alias_source,
                        )
                    )

        in_unit(self.factory, remember)
        return stored

    def code(self, product_id: int, identity: CodeIdentity, source: CodeSource) -> ProductCode:
        row = ProductCode(
            None,
            product_id,
            identity.scheme,
            identity.store_key,
            identity.code,
            source,
            CREATED_AT,
            CREATED_AT,
        )
        return in_unit(self.factory, lambda uow: uow.product_codes.upsert(row, replace=True))

    def correct(self, **overrides: Any) -> Correction:
        return in_unit(self.factory, lambda uow: uow.corrections.add(correction(**overrides)))

    def alias(self, product_id: int, **overrides: Any) -> ProductAlias:
        stored = product_alias(product_id, **overrides)
        return in_unit(self.factory, lambda uow: uow.products.add_alias(stored))


def _item(
    position: int,
    raw_text: str,
    product_id: int | None,
    cents: int = 899,
    source: MatchSource = MatchSource.NEW,
) -> LineItem:
    return item_line(
        position,
        raw_text,
        cents,
        product_id=product_id,
        match_source=None if product_id is None else source,
    )


def _post(name: str, source: FieldSource) -> Product:
    """A product made after 0006: only its name, with its source."""
    return product(
        canonical_name=name,
        brand=None,
        size_value=None,
        size_unit=None,
        category=None,
        tags=(),
        field_sources={"canonical_name": source},
    )


def _printed(before: Product, name: str) -> Product:
    return replace(
        before,
        canonical_name=name,
        brand=None,
        size_value=None,
        size_unit=None,
        category=None,
        tags=(),
        field_sources=PRINTED,
    )


def _codes(database: FakeDatabase) -> list[CodeRow]:
    return sorted(
        (row.product_id, row.scheme, row.store_key, row.code, row.source)
        for row in database.state.product_codes.values()
    )


def _owners(database: FakeDatabase) -> dict[tuple[int, int], int | None]:
    return {
        (receipt_id, item.position): item.product_id
        for receipt_id, stored in database.state.receipts.items()
        for item in stored.items
        if item.kind is LineKind.ITEM
    }


def _product(database: FakeDatabase, product_id: int) -> Product:
    return database.state.products[product_id]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def uow_factory(database: FakeDatabase, clock: FakeClock) -> UnitOfWorkFactory:
    return fake_uow_factory(database, clock)


@pytest.fixture
def legacy(uow_factory: UnitOfWorkFactory) -> Legacy:
    return Legacy(uow_factory)


@pytest.fixture
def plu(tmp_path: Path) -> PluCatalog:
    path = tmp_path / "plu.csv"
    path.write_text(PLU_CSV, encoding="utf-8")
    return PluCatalog(path)


@pytest.fixture
def backfill(uow_factory: UnitOfWorkFactory, clock: FakeClock, plu: PluCatalog) -> BackfillCatalog:
    return BackfillCatalog(uow_factory, clock, plu)


def _split_state(legacy: Legacy) -> tuple[int, Receipt]:
    """P (id 1) bought X, X, X, Y (1599 less a 400 discount) and U1 on one Costco receipt."""
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    receipt = legacy.buy(
        _item(0, C1, p),
        _item(1, C1, p),
        _item(2, C1, p),
        _item(3, C2, p, 1599),
        discount_line(4, "TPD/1543210", -400, 3),
        _item(5, U1, p),
    )
    return p, receipt


# --- dry run, one-code products, reset by source (PC-TC-125, 126, 127) ---------------------------


def test_pc_tc_125_a_dry_run_writes_nothing_and_reports_what_apply_does(
    legacy: Legacy,
    backfill: BackfillCatalog,
    database: FakeDatabase,
    clock: FakeClock,
    plu: PluCatalog,
) -> None:
    """A second splinter of Y (on Q) goes to the product the first splinter "created"."""
    p, first = _split_state(legacy)
    q = legacy.add(legacy_product(canonical_name="Baby Wipes"))
    legacy.buy(_item(0, L1, q), _item(1, L1, q), store=LOBLAWS)
    second = legacy.buy(_item(0, C2, q))
    before = copy.deepcopy(database.state)
    other = FakeDatabase(copy.deepcopy(database.state))

    dry = backfill()
    applied = BackfillCatalog(fake_uow_factory(other, clock), clock, plu)(apply=True)

    assert database.state == before
    assert (dry.applied, applied.applied) == (False, True)
    assert replace(dry, applied=True) == applied
    assert [
        (split.receipt_id, split.from_product_id, split.to_product_id) for split in dry.splits
    ] == [(require_id(first.id), p, None), (require_id(second.id), q, None)]
    (kiwi,) = [row for row in other.state.product_codes.values() if row.code == Y.code]
    owners = {
        key: owner
        for key, owner in _owners(other).items()
        if key in {(require_id(first.id), 3), (require_id(second.id), 0)}
    }
    assert set(owners.values()) == {kiwi.product_id}


def test_pc_tc_126_a_one_code_product_gets_its_code_and_its_printed_name(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    """The first line is the earliest by receipt date (stored second here), not by id."""
    p = legacy.add(
        legacy_product(canonical_name="Pampers Baby Wipes", brand="Pampers", tags=("wipes",))
    )
    legacy.buy(_item(0, "06038306487 PC WIPES 72CT HMRJ", p), store=LOBLAWS, on=date(2026, 11, 5))
    legacy.buy(_item(3, L1, p), store=LOBLAWS, on=date(2026, 11, 1))
    before = _product(database, p)
    untouched = copy.deepcopy(
        (database.state.receipts, database.state.prices, database.state.aliases)
    )

    found = backfill(apply=True)

    after = _printed(before, "PC UNS BABYWIPES")
    assert found == CatalogBackfill(
        products=(BackfilledProduct(p, WIPES, 2, before, after, True, True),),
        splits=(),
        conflicts=(),
        merge_candidates=(),
        codes_learned=0,
        aliases_renormalized=0,
        corrections_renormalized=0,
        applied=True,
    )
    assert (found.changes, _codes(database), _product(database, p)) == (
        1,
        [(p, CodeScheme.GTIN, "", WIPES.code, CodeSource.BACKFILL)],
        after,
    )
    assert (database.state.receipts, database.state.prices, database.state.aliases) == untouched


@pytest.mark.parametrize(
    "source",
    [FieldSource.LEGACY_MODEL, FieldSource.PERSON, FieldSource.PLU, FieldSource.RECEIPT_TEXT],
)
def test_pc_tc_127_each_field_is_reset_only_when_a_model_wrote_it(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase, source: FieldSource
) -> None:
    stored = legacy_product(canonical_name="Chocolate Pistachio Bars")
    p = legacy.add(replace(stored, field_sources=dict.fromkeys(FIELD_KEYS, source)))
    legacy.buy(_item(0, C1, p))
    before = _product(database, p)

    backfill(apply=True)

    expected = _printed(before, "CHOC PISTACH") if source is FieldSource.LEGACY_MODEL else before
    assert (_product(database, p), _codes(database)) == (
        expected,
        [(p, X.scheme, X.store_key, X.code, CodeSource.BACKFILL)],
    )


def test_pc_tc_127_a_printed_name_follows_the_final_lines_when_a_line_moves_in(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    """R (named ``OLD`` from its printed text, holding the 1111111 row) gains S's earlier
    ``1111111 NEW`` line: a tie of one line each goes to the earliest line, so R is re-named
    ``NEW``. A ``receipt_text`` name is kept only while the product's lines do not change."""
    r = legacy.add(_post("OLD", FieldSource.RECEIPT_TEXT))
    s = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    legacy.code(r, CodeIdentity(CodeScheme.STORE, STORE_KEY, "1111111"), CodeSource.MANUAL)
    legacy.buy(_item(0, C1, s), _item(1, C1, s), _item(2, "1111111 NEW", s))
    legacy.buy(_item(0, "2222222 OLD", r))

    backfill(apply=True)

    renamed = _product(database, r)
    assert (renamed.canonical_name, renamed.field_sources) == ("NEW", PRINTED)
    assert backfill(apply=True).changes == 0


def test_pc_tc_127_uncoded_unlinked_and_non_item_lines(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    """U: only uncoded lines; N: no line at all; P: a C1 item and a DEPOSIT line printed with
    C2's code, linked to P (only ITEM lines carry codes)."""
    u = legacy.add(legacy_product(canonical_name="Organic Eggs"))
    n = legacy.add(legacy_product(canonical_name="Never Bought"))
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    deposit = line_item(position=2, kind=LineKind.DEPOSIT, raw_text=C2, product_id=p)
    legacy.buy(_item(0, U1, u), _item(1, C1, p), deposit)
    before = {pid: _product(database, pid) for pid in (u, n, p)}
    owners = _owners(database)

    backfill(apply=True)

    assert {pid: _product(database, pid) for pid in (u, n, p)} == {
        u: _printed(before[u], "KS ORG EGGS 24CT"),
        n: before[n],
        p: _printed(before[p], "CHOC PISTACH"),
    }
    assert (_codes(database), _owners(database)) == (
        [(p, X.scheme, X.store_key, X.code, CodeSource.BACKFILL)],
        owners,
    )


# --- splits (PC-TC-128, 129, 130) ----------------------------------------------------------------


def test_pc_tc_128_lines_of_another_code_move_and_uncoded_lines_stay(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase, clock: FakeClock
) -> None:
    p, receipt = _split_state(legacy)
    rid = require_id(receipt.id)
    y_line = require_id(receipt.items[3].id)
    discounted = database.state.prices[
        next(k for k, row in database.state.prices.items() if row.line_item_id == y_line)
    ]

    found = backfill(apply=True)

    kiwi = max(database.state.products)
    assert found.splits == (BackfillSplit(rid, 3, Y, p, None, "GOLD KIWI"),)
    assert _product(database, kiwi) == Product(
        kiwi, "GOLD KIWI", None, None, None, None, (), clock.now(), PRINTED
    )
    assert _codes(database) == [
        (p, X.scheme, X.store_key, X.code, CodeSource.BACKFILL),
        (kiwi, Y.scheme, Y.store_key, Y.code, CodeSource.BACKFILL),
    ]
    assert _owners(database) == {(rid, 0): p, (rid, 1): p, (rid, 2): p, (rid, 3): kiwi, (rid, 5): p}
    moved = next(row for row in database.state.prices.values() if row.line_item_id == y_line)
    assert moved == replace(discounted, product_id=kiwi)
    assert (moved.discount.amount_cents, moved.paid.amount_cents, moved.on_sale) == (
        -400,
        1199,
        True,
    )
    assert database.state.corrections == {}


def test_pc_tc_129_a_a_line_moves_to_the_product_its_code_already_names(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    k = legacy.add(_post("Gold Kiwi", FieldSource.PERSON))
    legacy.code(k, Y, CodeSource.MANUAL)
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    receipt = legacy.buy(_item(0, C1, p), _item(1, C1, p), _item(2, C2, p))
    rid = require_id(receipt.id)

    backfill(apply=True)

    assert (sorted(database.state.products), _owners(database), _codes(database)) == (
        [k, p],
        {(rid, 0): p, (rid, 1): p, (rid, 2): k},
        [
            (k, Y.scheme, Y.store_key, Y.code, CodeSource.MANUAL),
            (p, X.scheme, X.store_key, X.code, CodeSource.BACKFILL),
        ],
    )


def test_pc_tc_129_b_stray_lines_of_one_code_end_on_one_product(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    q = legacy.add(legacy_product(canonical_name="Baby Wipes"))
    first = legacy.buy(_item(0, C1, p), _item(1, C1, p), _item(2, C2, p))
    legacy.buy(_item(0, L1, q), _item(1, L1, q), store=LOBLAWS)
    second = legacy.buy(_item(0, C2, q))

    backfill(apply=True)

    kiwi = [row.product_id for row in database.state.product_codes.values() if row.code == Y.code]
    assert len(kiwi) == 1
    owners = _owners(database)
    assert (owners[(require_id(first.id), 2)], owners[(require_id(second.id), 0)]) == (
        kiwi[0],
        kiwi[0],
    )
    assert len(database.state.products) == 3
    assert _product(database, kiwi[0]).canonical_name == "GOLD KIWI"


@pytest.mark.parametrize(
    ("order", "kept"),
    [((C1, C2, C1, C2), X), ((C2, C1, C2, C1), Y)],
    ids=["x-first", "y-first"],
)
def test_pc_tc_129_c_a_tie_keeps_the_code_of_the_earliest_line(
    legacy: Legacy,
    backfill: BackfillCatalog,
    database: FakeDatabase,
    order: tuple[str, ...],
    kept: CodeIdentity,
) -> None:
    """The earliest receipt is stored last, so neither id nor insertion order decides; the
    latest line carries the other code."""
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    for day, raw_text in reversed(list(enumerate(order))):
        legacy.buy(_item(0, raw_text, p), on=FIRST_DAY + timedelta(days=day))

    found = backfill(apply=True)

    assert [row for row in _codes(database) if row[0] == p] == [
        (p, kept.scheme, kept.store_key, kept.code, CodeSource.BACKFILL)
    ]
    assert len(found.splits) == 2


@pytest.mark.parametrize(
    ("first", "second", "kept", "moved"),
    [
        (("3101 TST APPLE", STORE_NAME), (L3, LOBLAWS),
         COSTCO_APPLE, APPLE),
        ((L3, LOBLAWS), (L4, LOBLAWS), APPLE, ORGANIC_APPLE),
    ],
    ids=["d-profile-of-each-receipt", "e-organic-is-its-own-code"],
)  # fmt: skip
def test_pc_tc_129_d_e_codes_are_read_with_each_lines_own_store(  # noqa: PLR0913, PLR0917 - one row
    legacy: Legacy,
    backfill: BackfillCatalog,
    database: FakeDatabase,
    first: tuple[str, str],
    second: tuple[str, str],
    kept: CodeIdentity,
    moved: CodeIdentity,
) -> None:
    p = legacy.add(legacy_product(canonical_name="Test Apples"))
    legacy.buy(_item(0, first[0], p), store=first[1])
    later = legacy.buy(_item(0, second[0], p), store=second[1])

    backfill(apply=True)

    target = _owners(database)[(require_id(later.id), 0)]
    assert target not in {p, None}
    assert sorted((row[0], row[1:4]) for row in _codes(database)) == sorted(
        [
            (p, (kept.scheme, kept.store_key, kept.code)),
            (target, (moved.scheme, moved.store_key, moved.code)),
        ]
    )


def test_pc_tc_129_f_one_global_code_bought_at_two_stores_does_not_split(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    p = legacy.add(legacy_product(canonical_name="Baby Wipes"))
    legacy.buy(_item(0, L1, p), store=LOBLAWS)
    legacy.buy(_item(0, L1, p), store=WALMART)
    owners = _owners(database)

    found = backfill(apply=True)

    assert (found.splits, _owners(database), _codes(database)) == (
        (),
        owners,
        [(p, WIPES.scheme, "", WIPES.code, CodeSource.BACKFILL)],
    )


@pytest.mark.parametrize(
    ("match_source", "alias_source", "reason"),
    [
        (MatchSource.MEMORY, AliasSource.MATCHED, PersonLink.CORRECTION),
        (MatchSource.NEW, AliasSource.IMPORT, PersonLink.IMPORT_ALIAS),
        (MatchSource.NEW, AliasSource.MATCHED, None),
    ],
    ids=["memory", "import-alias", "control-matched-alias"],
)
def test_pc_tc_130_a_person_linked_line_never_moves(  # noqa: PLR0913, PLR0917 - one row
    legacy: Legacy,
    backfill: BackfillCatalog,
    database: FakeDatabase,
    match_source: MatchSource,
    alias_source: AliasSource,
    reason: PersonLink | None,
) -> None:
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    legacy.buy(_item(0, C1, p), _item(1, C1, p))
    stray = legacy.buy(_item(0, C2, p, source=match_source), alias_source=alias_source)
    rid = require_id(stray.id)

    found = backfill(apply=True)

    if reason is None:
        assert (found.conflicts, _owners(database)[(rid, 0)] != p) == ((), True)
        return
    assert found.conflicts == (BackfillConflict(rid, 0, p, Y, reason),)
    assert _owners(database)[(rid, 0)] == p
    assert _codes(database) == [(p, X.scheme, X.store_key, X.code, CodeSource.BACKFILL)]


# --- code learning, merge candidates (PC-TC-131, 132) -------------------------------------------


def test_pc_tc_131_codes_are_learned_from_the_newest_correction_first(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    a = legacy.add(_post("Choc Bars A", FieldSource.PERSON))
    b = legacy.add(_post("Choc Bars B", FieldSource.PERSON))
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    legacy.buy(_item(0, C1, p), _item(1, C1, p), _item(2, C1, p))
    corrected = legacy.buy(_item(0, C1, b, source=MatchSource.MEMORY))
    line = require_id(corrected.items[0].id)
    for target, minutes in ((a, 1), (b, 2)):
        legacy.correct(
            kind=CorrectionKind.PRODUCT_LINK,
            subject="choc pistach",
            new_value=str(target),
            line_item_id=line,
            receipt_id=corrected.id,
            created_at=at(minutes),
        )

    owners = _owners(database)

    found = backfill(apply=True)

    assert (found.codes_learned, found.merge_candidates, _codes(database)) == (
        1,
        (MergeCandidate(X, b, p),),
        [(b, X.scheme, X.store_key, X.code, CodeSource.CORRECTION)],
    )
    assert (_owners(database), found.splits) == (owners, ())


@pytest.mark.parametrize("minutes", [-60, 60], ids=["correction-older", "correction-newer"])
def test_pc_tc_131_learning_never_replaces_a_persons_code_row(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase, minutes: int
) -> None:
    b = legacy.add(_post("Choc Bars B", FieldSource.PERSON))
    c = legacy.add(_post("Choc Bars C", FieldSource.PERSON))
    row = legacy.code(c, X, CodeSource.MANUAL)
    corrected = legacy.buy(_item(0, C1, b, source=MatchSource.MEMORY))
    legacy.correct(
        kind=CorrectionKind.PRODUCT_LINK,
        subject="choc pistach",
        new_value=str(b),
        line_item_id=require_id(corrected.items[0].id),
        receipt_id=corrected.id,
        created_at=at(minutes),
    )

    found = backfill(apply=True)

    assert (found.codes_learned, list(database.state.product_codes.values())) == (0, [row])


def test_pc_tc_132_two_products_of_one_code_are_reported_never_merged(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    p1 = legacy.add(legacy_product(canonical_name="Choc Bars"))
    p2 = legacy.add(legacy_product(canonical_name="Pistachio Bars"))
    legacy.buy(_item(0, C1, p1), _item(1, C1, p2))
    counts = tuple(len(table) for table in (database.state.products, database.state.prices))
    owners = _owners(database)

    found = backfill(apply=True)

    assert found.merge_candidates == (MergeCandidate(X, p1, p2),)
    assert _codes(database) == [(p1, X.scheme, X.store_key, X.code, CodeSource.BACKFILL)]
    assert (len(database.state.products), len(database.state.prices), _owners(database)) == (
        *counts,
        owners,
    )


# --- person values, PLU, vectors (PC-TC-133, 134, 135) ------------------------------------------

type Arrange = Callable[[Legacy, int, Receipt], object]


def _link(legacy: Legacy, p: int, bought: Receipt, *, seconds: int = 0) -> object:
    return legacy.correct(
        kind=CorrectionKind.PRODUCT_LINK,
        subject="choc pistach",
        old_value=None,
        new_value=str(p),
        line_item_id=require_id(bought.items[0].id),
        created_at=CREATED_AT + timedelta(seconds=seconds),
    )


def _categories(legacy: Legacy, store_key: str) -> object:
    for minutes, value in ((1, Category.PANTRY), (2, Category.SNACKS_CANDY)):
        legacy.correct(
            subject="choc pistach",
            new_value=value.value,
            store_key=store_key,
            created_at=at(minutes),
        )
    return None


PERSON_VALUES: list[tuple[str, Arrange, str, FieldSource, Category | None]] = [
    ("a-created-by-a-correction", _link, "Chocolate Pistachio Bars", FieldSource.PERSON, None),
    ("b-correction-a-second-later", lambda lg, p, r: _link(lg, p, r, seconds=1), "CHOC PISTACH",
     FieldSource.RECEIPT_TEXT, None),
    ("c-import-alias", lambda lg, p, _: lg.alias(p, alias="choc pistach",
     source=AliasSource.IMPORT), "Chocolate Pistachio Bars", FieldSource.PERSON, None),
    ("c-matched-alias", lambda *_: None, "CHOC PISTACH", FieldSource.RECEIPT_TEXT, None),
    ("d-newest-category-correction", lambda lg, *_: _categories(lg, STORE_KEY), "CHOC PISTACH",
     FieldSource.RECEIPT_TEXT, Category.SNACKS_CANDY),
    ("e-category-at-another-store", lambda lg, *_: _categories(lg, "loblaws"), "CHOC PISTACH",
     FieldSource.RECEIPT_TEXT, None),
]  # fmt: skip


@pytest.mark.parametrize(
    ("arrange", "name", "name_source", "category"),
    [row[1:] for row in PERSON_VALUES],
    ids=[row[0] for row in PERSON_VALUES],
)
def test_pc_tc_133_a_persons_name_and_category_are_kept_as_person(  # noqa: PLR0913, PLR0917 - one row
    legacy: Legacy,
    backfill: BackfillCatalog,
    database: FakeDatabase,
    arrange: Arrange,
    name: str,
    name_source: FieldSource,
    category: Category | None,
) -> None:
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    arrange(legacy, p, legacy.buy(_item(0, C1, p), _item(1, C1, p)))

    backfill(apply=True)

    sources = {"canonical_name": name_source}
    if category is not None:
        sources["category"] = FieldSource.PERSON
    assert _product(database, p) == Product(
        p, name, None, None, None, category, (), CREATED_AT, sources
    )


PLU_ROWS: list[tuple[str, str, str, bool, Product, CodeIdentity]] = [
    ("l3", L3, LOBLAWS, False,
     _post("Test Apple", FieldSource.PLU), APPLE),
    ("l4-organic", L4, LOBLAWS, False,
     replace(_post("Organic Test Apple", FieldSource.PLU), tags=("organic",)), ORGANIC_APPLE),
    ("l3-person-named", L3, LOBLAWS, True,
     _post("Chocolate Pistachio Bars", FieldSource.PERSON), APPLE),
    ("costco-3101-is-a-store-code", "3101 TST APPLE", STORE_NAME, False,
     _post("TST APPLE", FieldSource.RECEIPT_TEXT), COSTCO_APPLE),
]  # fmt: skip


@pytest.mark.parametrize(
    ("raw_text", "store", "person_named", "expected", "code"),
    [row[1:] for row in PLU_ROWS],
    ids=[row[0] for row in PLU_ROWS],
)
def test_pc_tc_134_a_plu_code_fills_what_no_person_kept(  # noqa: PLR0913, PLR0917 - one table row
    legacy: Legacy,
    backfill: BackfillCatalog,
    database: FakeDatabase,
    raw_text: str,
    store: str,
    person_named: bool,
    expected: Product,
    code: CodeIdentity,
) -> None:
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    bought = legacy.buy(_item(0, raw_text, p), store=store)
    if person_named:
        legacy.correct(
            kind=CorrectionKind.PRODUCT_LINK,
            subject=normalize_alias(raw_text),
            new_value=str(p),
            store_key=bought.store_key,
            created_at=CREATED_AT,
        )

    backfill(apply=True)

    sources = dict(expected.field_sources)
    if code.scheme is CodeScheme.PLU:
        sources["category"] = FieldSource.PLU
        if expected.tags:
            sources["tags"] = FieldSource.PLU
    category = Category.PRODUCE if code.scheme is CodeScheme.PLU else None
    assert _product(database, p) == replace(
        expected, id=p, category=category, field_sources=sources
    )
    assert _codes(database) == [(p, code.scheme, code.store_key, code.code, CodeSource.BACKFILL)]


def test_pc_tc_134_without_plu_csv_names_stay_printed_with_one_warning(
    legacy: Legacy,
    uow_factory: UnitOfWorkFactory,
    clock: FakeClock,
    database: FakeDatabase,
    tmp_path: Path,
) -> None:
    apple = legacy.add(legacy_product(canonical_name="Apples"))
    organic = legacy.add(legacy_product(canonical_name="Organic Apples"))
    legacy.buy(_item(0, L3, apple), _item(1, L4, organic), store=LOBLAWS)

    with capture_logs() as logs:
        BackfillCatalog(uow_factory, clock, PluCatalog(tmp_path / "absent.csv"))(apply=True)

    assert [
        (_product(database, pid).canonical_name, _product(database, pid).field_sources)
        for pid in (apple, organic)
    ] == [("TST APPLE", PRINTED), ("ORG TST APPLE", PRINTED)]
    assert [log["event"] for log in logs].count("catalog.missing") == 1


def test_pc_tc_135_only_unconfirmed_products_lose_their_vector(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    q = legacy.add(_post("Gold Kiwi", FieldSource.PERSON))
    r = legacy.add(legacy_product(canonical_name="Apples"))
    legacy.buy(_item(0, C1, p), _item(1, C2, q))
    legacy.buy(_item(0, L3, r), store=LOBLAWS)
    kept = {pid: database.state.vectors[pid] for pid in (q, r)}

    backfill(apply=True)

    assert database.state.vectors == kept


# --- aliases (PC-TC-136, 137) --------------------------------------------------------------------


def test_pc_tc_136_the_matched_alias_of_a_moved_line_follows_it(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    p, _ = _split_state(legacy)
    person = legacy.alias(p, alias="chocolate bars", source=AliasSource.USER_CORRECTION)
    imported = legacy.alias(p, alias="pistachio", source=AliasSource.IMPORT)
    before = copy.deepcopy(database.state.aliases)
    kiwi = next(a for a in before.values() if a.alias == "gold kiwi")

    backfill(apply=True)

    moved_to = next(r.product_id for r in database.state.product_codes.values() if r.code == Y.code)
    assert database.state.aliases == {
        **before,
        require_id(kiwi.id): replace(kiwi, product_id=moved_to),
    }
    assert (person, imported) == tuple(
        database.state.aliases[require_id(alias.id)] for alias in (person, imported)
    )


@pytest.mark.parametrize(
    ("source", "kept"),
    [(AliasSource.MATCHED, False), (AliasSource.USER_CORRECTION, True)],
    ids=["matched-deleted", "person-kept"],
)
def test_pc_tc_136_an_alias_shared_by_staying_and_moved_lines(
    legacy: Legacy,
    backfill: BackfillCatalog,
    database: FakeDatabase,
    source: AliasSource,
    kept: bool,
) -> None:
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    receipt = legacy.buy(
        _item(0, C1, p), _item(1, C1, p), _item(2, "1543210 CHOC PISTACH", p), alias_source=source
    )
    before = copy.deepcopy(database.state.aliases)

    backfill(apply=True)

    assert _owners(database)[(require_id(receipt.id), 2)] != p
    assert database.state.aliases == (before if kept else {})


def test_pc_tc_137_stored_keys_are_rewritten_to_the_new_normalization(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    a = legacy.add(_post("Wipes", FieldSource.PERSON))
    old = legacy.alias(a, alias="pc uns babywipes hmrj", store_key="loblaws")
    pj = legacy.alias(a, alias="th 2pc pj")
    subject = legacy.correct(subject="tst apple mrj", store_key="loblaws", new_value="produce")

    found = backfill(apply=True)

    assert (found.aliases_renormalized, found.corrections_renormalized) == (1, 1)
    assert database.state.aliases == {
        require_id(old.id): replace(old, alias="pc uns babywipes"),
        require_id(pj.id): pj,
    }
    assert database.state.corrections == {
        require_id(subject.id): replace(subject, subject="tst apple")
    }


ALIAS_COLLISIONS = [
    ("matched-old-newer-vs-person-new", AliasSource.MATCHED, 2, AliasSource.USER_CORRECTION, 1,
     "new"),
    ("person-old-vs-matched-new-newer", AliasSource.USER_CORRECTION, 1, AliasSource.MATCHED, 2,
     "old"),
    ("two-matched-new-newer", AliasSource.MATCHED, 1, AliasSource.MATCHED, 2, "new"),
    ("two-matched-old-newer", AliasSource.MATCHED, 2, AliasSource.MATCHED, 1, "old"),
    ("two-person-old-newer", AliasSource.IMPORT, 2, AliasSource.USER_CORRECTION, 1, "old"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("old_source", "old_minutes", "new_source", "new_minutes", "survivor"),
    [row[1:] for row in ALIAS_COLLISIONS],
    ids=[row[0] for row in ALIAS_COLLISIONS],
)
def test_pc_tc_137_an_alias_collision_keeps_one_row(  # noqa: PLR0913, PLR0917 - one table row
    legacy: Legacy,
    backfill: BackfillCatalog,
    database: FakeDatabase,
    old_source: AliasSource,
    old_minutes: int,
    new_source: AliasSource,
    new_minutes: int,
    survivor: str,
) -> None:
    a = legacy.add(_post("Wipes A", FieldSource.PERSON))
    b = legacy.add(_post("Wipes B", FieldSource.PERSON))
    rows = {
        "old": legacy.alias(
            a,
            alias="pc uns babywipes hmrj",
            store_key="loblaws",
            source=old_source,
            created_at=at(old_minutes),
        ),
        "new": legacy.alias(
            b,
            alias="pc uns babywipes",
            store_key="loblaws",
            source=new_source,
            created_at=at(new_minutes),
        ),
    }

    backfill(apply=True)

    assert list(database.state.aliases.values()) == [
        replace(rows[survivor], alias="pc uns babywipes")
    ]


def test_pc_tc_137_colliding_corrections_are_rekeyed_and_both_kept(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    """Corrections have no unique key and ``latest`` picks the newest, so none is deleted."""
    old = legacy.correct(subject="tst apple mrj", store_key="loblaws", created_at=at(2))
    new = legacy.correct(subject="tst apple", store_key="loblaws", created_at=at(1))

    backfill(apply=True)

    assert list(database.state.corrections.values()) == [replace(old, subject="tst apple"), new]


type Seeded = tuple[CorrectionKind, str, str, int]
FALLBACK_STATES: list[tuple[str, list[Seeded]]] = [
    ("only-old-key", [(CorrectionKind.CATEGORY, "tst apple mrj", "loblaws", 1)]),
    ("only-new-key", [(CorrectionKind.CATEGORY, "tst apple", "loblaws", 1)]),
    ("old-key-newer", [(CorrectionKind.CATEGORY, "tst apple mrj", "loblaws", 2),
                       (CorrectionKind.CATEGORY, "tst apple", "loblaws", 1)]),
    ("new-key-newer", [(CorrectionKind.CATEGORY, "tst apple mrj", "loblaws", 1),
                       (CorrectionKind.CATEGORY, "tst apple", "loblaws", 2)]),
    ("kinds-independent", [(CorrectionKind.CATEGORY, "tst apple mrj", "loblaws", 2),
                           (CorrectionKind.PRODUCT_LINK, "tst apple", "loblaws", 1)]),
    ("stores-separate", [(CorrectionKind.CATEGORY, "tst apple mrj", "walmart", 2),
                         (CorrectionKind.CATEGORY, "tst apple", "loblaws", 1)]),
]  # fmt: skip


@pytest.mark.parametrize(
    "seeded", [row[1] for row in FALLBACK_STATES], ids=[row[0] for row in FALLBACK_STATES]
)
def test_pc_tc_137_the_correction_found_for_a_line_is_the_same_after(
    legacy: Legacy,
    backfill: BackfillCatalog,
    uow_factory: UnitOfWorkFactory,
    seeded: list[Seeded],
) -> None:
    """PC-TC-92's states, for the line ``3101 TST APPLE MRJ`` at ``loblaws``."""
    for index, (kind, subject, store_key, minutes) in enumerate(seeded):
        legacy.correct(
            kind=kind,
            subject=subject,
            store_key=store_key,
            new_value=f"value {index}",
            created_at=at(minutes),
        )

    def found() -> list[tuple[str, object] | None]:
        with uow_factory(read_only=True) as uow:
            hits = [
                latest_correction(uow, kind, L3, "loblaws")
                for kind in (CorrectionKind.CATEGORY, CorrectionKind.PRODUCT_LINK)
            ]
        return [None if hit is None else (hit.new_value, hit.created_at) for hit in hits]

    before = found()
    backfill(apply=True)

    assert found() == before


@pytest.mark.parametrize(
    ("older_new_key", "expected"),
    [(None, Category.DAIRY_EGGS), (Category.PANTRY, Category.DAIRY_EGGS)],
    ids=["only-old-key", "old-key-newer-than-new-key"],
)
def test_i20_a_correction_under_an_old_key_applies_to_a_line_without_the_suffix(
    legacy: Legacy,
    backfill: BackfillCatalog,
    database: FakeDatabase,
    older_new_key: Category | None,
    expected: Category,
) -> None:
    """``1111 MILK`` has no tax flag, so neither normalization of its text is the stored key
    ``milk hmrj``; the correction still applies on the first apply, over an older one already
    under ``milk``, and the second apply has nothing left to do."""
    p = legacy.add(legacy_product(canonical_name="Milk 4 L"))
    legacy.buy(_item(0, "1111 MILK", p))
    if older_new_key is not None:
        legacy.correct(subject="milk", new_value=older_new_key.value, created_at=at(1))
    legacy.correct(subject="milk hmrj", new_value=expected.value, created_at=at(2))

    backfill(apply=True)
    first = _product(database, p)
    second = backfill(apply=True)

    assert (first.category, first.field_sources.get("category")) == (
        expected,
        FieldSource.PERSON,
    )
    assert (second.changes, _product(database, p)) == (0, first)


# --- scale, nothing to do, idempotence, catalog-off products (PC-TC-139, 142, 158, 171) ---------


def test_pc_tc_139_a_second_apply_relinks_nothing_and_writes_nothing(
    legacy: Legacy,
    backfill: BackfillCatalog,
    database: FakeDatabase,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _split_state(legacy)
    backfill(apply=True)
    clock.advance(timedelta(hours=1))
    before = copy.deepcopy(database.state)
    calls: list[int] = []
    link_items = FakeReceiptRepository.link_items

    def recording(self: FakeReceiptRepository, receipt_id: int, links: Any) -> Receipt:
        calls.append(receipt_id)
        return link_items(self, receipt_id, links)

    monkeypatch.setattr(FakeReceiptRepository, "link_items", recording)

    second = backfill(apply=True)

    assert (second.changes, calls, database.state) == (0, [], before)


def test_pc_tc_142_every_product_is_visited(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    """``products.ids()`` has no paging (its contract test owns that); three products pin that
    every one is reached, not only the first."""
    ids = [legacy.add(legacy_product(canonical_name=f"Thing {n}"), vector=False) for n in range(3)]
    legacy.buy(*(_item(n, f"{2000000 + n} THING {n}", pid) for n, pid in enumerate(ids)))

    backfill(apply=True)

    assert _codes(database) == [
        (pid, CodeScheme.STORE, STORE_KEY, str(2000000 + n), CodeSource.BACKFILL)
        for n, pid in enumerate(ids)
    ]
    assert {pid: _product(database, pid).field_sources for pid in ids} == dict.fromkeys(
        ids, PRINTED
    )


def _post_0006(legacy: Legacy) -> None:
    named = legacy.add(_post("Choc Bars", FieldSource.PERSON))
    printed = legacy.add(_post("GOLD KIWI", FieldSource.RECEIPT_TEXT), vector=False)
    legacy.code(named, X, CodeSource.MANUAL)
    legacy.code(printed, Y, CodeSource.RECEIPT)
    legacy.buy(
        _item(0, C1, named, source=MatchSource.CATALOG),
        _item(1, C2, printed, source=MatchSource.CATALOG),
    )


@pytest.mark.parametrize(
    "arrange", [lambda _: None, _post_0006], ids=["no-products", "only-post-0006-products"]
)
def test_pc_tc_158_nothing_to_do_is_0_changes(
    legacy: Legacy,
    backfill: BackfillCatalog,
    database: FakeDatabase,
    arrange: Callable[[Legacy], None],
) -> None:
    arrange(legacy)
    before = copy.deepcopy(database.state)

    found = backfill(apply=True)

    assert (found.changes, found.products, database.state) == (0, (), before)


def test_pc_tc_171_a_product_made_with_the_catalog_off_gets_its_code(
    legacy: Legacy,
    backfill: BackfillCatalog,
    database: FakeDatabase,
    uow_factory: UnitOfWorkFactory,
    clock: FakeClock,
) -> None:
    p = legacy.add(_post("CHOC PISTACH", FieldSource.RECEIPT_TEXT), vector=False)
    legacy.buy(_item(0, C1, p), _item(1, C1, p))
    product_row, aliases = _product(database, p), copy.deepcopy(database.state.aliases)

    first = backfill(apply=True)
    second = backfill(apply=True)

    assert (first.changes, second.changes) == (1, 0)
    assert (_codes(database), _product(database, p), database.state.aliases) == (
        [(p, X.scheme, X.store_key, X.code, CodeSource.BACKFILL)],
        product_row,
        aliases,
    )
    clock.set(NOW)
    harness = build_harness(
        ScriptedLlm(coded_extraction([C1], store_name=STORE_NAME)),
        uow_factory=uow_factory,
        clock=clock,
    )
    receipt_id = harness.process_image("costco-after-backfill").receipt_id
    assert (harness.line(receipt_id, 0).product_id, harness.match_sources(receipt_id)) == (
        p,
        [MatchSource.CATALOG],
    )


def test_pc_tc_171_when_the_code_names_another_product_nothing_moves(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    p = legacy.add(_post("CHOC PISTACH", FieldSource.RECEIPT_TEXT), vector=False)
    n = legacy.add(_post("CHOC PISTACH", FieldSource.RECEIPT_TEXT), vector=False)
    legacy.code(n, X, CodeSource.RECEIPT)
    legacy.buy(_item(0, C1, p), _item(1, C1, p))
    legacy.buy(_item(0, C1, n, source=MatchSource.CATALOG))
    before = copy.deepcopy(database.state)

    first = backfill(apply=True)
    second = backfill(apply=True)

    assert (first.merge_candidates, second.changes, database.state) == (
        (MergeCandidate(X, n, p),),
        0,
        before,
    )


# --- review round 1: evidence order, created destinations, the kept code ------------------------


def test_pc_tc_130_133_an_import_alias_that_loses_a_key_collision_still_counts(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    """P's stray line and name are a person's only through an IMPORT alias under the old key
    (``stray hmrj``); a newer IMPORT alias under the new key (``stray``) names Q. Neither product
    has ``person`` provenance before the run."""
    p = legacy.add(legacy_product(canonical_name="My Chosen Name"))
    q = legacy.add(legacy_product(canonical_name="Something Else"))
    legacy.buy(_item(0, C1, p), _item(1, C1, p))
    stray = legacy.buy(_item(0, "1543210 STRAY HMRJ", p))
    legacy.alias(q, alias="stray", source=AliasSource.IMPORT, created_at=at(-1))
    legacy.alias(p, alias="stray hmrj", source=AliasSource.IMPORT, created_at=at(-2))
    rid = require_id(stray.id)

    found = backfill(apply=True)

    named = _product(database, p)
    assert (named.canonical_name, named.field_sources["canonical_name"]) == (
        "My Chosen Name",
        FieldSource.PERSON,
    )
    assert found.conflicts == (BackfillConflict(rid, 0, p, Y, PersonLink.IMPORT_ALIAS),)
    assert (_owners(database)[(rid, 0)], found.splits) == (p, ())


def test_i19_a_line_whose_import_alias_loses_its_key_keeps_its_link_as_memory(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    """The losing alias row goes; the line it protected stays on P as ``memory``, so a second
    apply (which no longer sees the alias) still leaves it and changes nothing."""
    p = legacy.add(legacy_product(canonical_name="My Chosen Name"))
    q = legacy.add(legacy_product(canonical_name="Something Else"))
    legacy.buy(_item(0, C1, p), _item(1, C1, p))
    stray = legacy.buy(_item(0, "1543210 STRAY HMRJ", p))
    legacy.alias(q, alias="stray", source=AliasSource.IMPORT, created_at=at(-1))
    lost = legacy.alias(p, alias="stray hmrj", source=AliasSource.IMPORT, created_at=at(-2))
    rid = require_id(stray.id)

    backfill(apply=True)

    (line,) = database.state.receipts[rid].items
    assert (line.product_id, line.match_source) == (p, MatchSource.MEMORY)
    assert require_id(lost.id) not in database.state.aliases
    second = backfill(apply=True)
    assert second.changes == 0
    assert database.state.receipts[rid].items[0].product_id == p


def test_pc_tc_129_b_139_a_created_product_is_described_from_every_line_moved_into_it(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    """Two stray lines of one new code with different texts; a person's CATEGORY correction is
    for the second text only. The first apply sets it; the second has nothing left to do."""
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    bought = legacy.buy(
        _item(0, C1, p),
        _item(1, C1, p),
        _item(2, C1, p),
        _item(3, "1543210 KIWI A", p),
        _item(4, "1543210 KIWI B", p),
    )
    legacy.correct(subject="kiwi b", new_value=Category.PRODUCE.value, created_at=at(1))
    rid = require_id(bought.id)

    backfill(apply=True)

    owners = _owners(database)
    made = _product(database, require_id(owners[(rid, 3)]))
    assert owners[(rid, 4)] == made.id
    assert (made.canonical_name, made.category, made.field_sources) == (
        "KIWI A",
        Category.PRODUCE,
        {"canonical_name": FieldSource.RECEIPT_TEXT, "category": FieldSource.PERSON},
    )
    assert backfill(apply=True).changes == 0


def test_pc_tc_126_the_name_comes_from_the_kept_codes_earliest_line(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    """The earliest line is uncoded; the name is the earliest X line's printed text."""
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    legacy.buy(_item(0, U1, p))
    legacy.buy(_item(0, C1, p), _item(1, C1, p))

    backfill(apply=True)

    assert (_product(database, p).canonical_name, _codes(database)) == (
        "CHOC PISTACH",
        [(p, X.scheme, X.store_key, X.code, CodeSource.BACKFILL)],
    )


def test_pc_tc_129_131_a_product_owns_a_learned_code_and_its_main_code(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    """A correction teaches Y to P (``R(Y) = P``). P's main code is X, the most frequent code
    of its lines it holds no row for, although more of its lines carry Y. Nothing moves: P owns
    both codes and is named from its most frequent final code, Y (I24)."""
    p = legacy.add(legacy_product(canonical_name="Kiwis"))
    legacy.buy(_item(0, C1, p), _item(1, C1, p))
    legacy.buy(_item(0, C2, p), _item(1, C2, p), _item(2, C2, p))
    corrected = legacy.buy(_item(0, C2, p, source=MatchSource.MEMORY))
    legacy.correct(
        kind=CorrectionKind.PRODUCT_LINK,
        subject="gold kiwi",
        new_value=str(p),
        receipt_id=corrected.id,
        line_item_id=require_id(corrected.items[0].id),
        created_at=at(1),
    )
    owners = _owners(database)

    found = backfill(apply=True)

    assert (_owners(database), found.splits, len(database.state.products)) == (owners, (), 1)
    assert _codes(database) == sorted(
        [
            (p, Y.scheme, Y.store_key, Y.code, CodeSource.CORRECTION),
            (p, X.scheme, X.store_key, X.code, CodeSource.BACKFILL),
        ]
    )
    assert _product(database, p).canonical_name == "GOLD KIWI"


# --- one owner per code (I23), shared aliases, report counts -----------------------


@pytest.mark.parametrize("owner_first", [False, True], ids=["owner-higher-id", "owner-lower-id"])
def test_i23_a_moved_line_goes_to_the_product_that_keeps_its_code(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase, owner_first: bool
) -> None:
    if owner_first:
        b = legacy.add(legacy_product(canonical_name="Kiwis"))
        a = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    else:
        a = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
        b = legacy.add(legacy_product(canonical_name="Kiwis"))
    legacy.buy(_item(0, C2, b), _item(1, C2, b), _item(2, C2, b))
    mixed = legacy.buy(_item(0, C1, a), _item(1, C1, a), _item(2, "1543210 GOLD KIWI 2LB", a))

    backfill(apply=True)

    assert _owners(database)[(require_id(mixed.id), 2)] == b
    assert _codes(database) == sorted(
        [
            (a, X.scheme, X.store_key, X.code, CodeSource.BACKFILL),
            (b, Y.scheme, Y.store_key, Y.code, CodeSource.BACKFILL),
        ]
    )
    assert len(database.state.products) == 2


def test_i23_a_tie_between_two_keepers_goes_to_the_lowest_id(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    first = legacy.add(legacy_product(canonical_name="First"))
    second = legacy.add(legacy_product(canonical_name="Second"))
    legacy.buy(_item(0, C1, second), _item(1, C1, first))

    found = backfill(apply=True)

    assert found.merge_candidates == (MergeCandidate(X, first, second),)
    assert _codes(database) == [(first, X.scheme, X.store_key, X.code, CodeSource.BACKFILL)]


def test_pc_tc_136_a_shared_alias_is_deleted_when_only_one_line_carries_a_tax_flag(
    legacy: Legacy, backfill: BackfillCatalog, uow_factory: UnitOfWorkFactory
) -> None:
    """``5555555 MILK`` stays (earliest line), ``6666666 MILK HMRJ`` moves, the uncoded ``MILK``
    stays: the MATCHED alias ``milk`` names both a staying and a moved line, so it goes, and the
    next uncoded ``MILK`` links to no product by it."""
    p = legacy.add(legacy_product(canonical_name="Milk"))
    legacy.buy(_item(0, "5555555 MILK", p), _item(1, "MILK", p), _item(2, "6666666 MILK HMRJ", p))

    backfill(apply=True)

    with uow_factory(read_only=True) as uow:
        assert uow.products.alias_at("milk", STORE_KEY) is None
        assert find_product(uow, "MILK", STORE_KEY) is None


def test_backfill_report_counts_a_conflict_once_and_a_destinations_final_lines(
    legacy: Legacy, backfill: BackfillCatalog
) -> None:
    """D (lower id) keeps Y and has a ``memory`` X line (one conflict); P keeps X and moves its Y
    line to D in P's group. The report has one conflict, and D's row counts its lines after the
    move (2 Y lines + the move; the conflict line stays too)."""
    d = legacy.add(legacy_product(canonical_name="Kiwis"))
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    held = legacy.buy(_item(0, C2, d), _item(1, C2, d), _item(2, C1, d, source=MatchSource.MEMORY))
    legacy.buy(_item(0, C1, p), _item(1, C1, p), _item(2, C2, p))

    found = backfill(apply=True)

    assert found.conflicts == (
        BackfillConflict(require_id(held.id), 2, d, X, PersonLink.CORRECTION),
    )
    lines = {row.product_id: row.lines for row in found.products}
    assert (lines[d], lines[p]) == (4, 2)


def test_i23b_a_person_linked_line_never_makes_its_code_attached(
    legacy: Legacy, backfill: BackfillCatalog, database: FakeDatabase
) -> None:
    """P's main code ignores the ``memory`` Y line: X is attached, the Y line stays as a
    conflict, Y is attached nowhere (no product is made for it), and a second apply changes
    nothing."""
    p = legacy.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    legacy.buy(_item(0, C1, p), _item(1, C1, p))
    stray = legacy.buy(_item(0, C2, p, source=MatchSource.MEMORY))

    first = backfill(apply=True)
    second = backfill(apply=True)

    assert first.conflicts == (
        BackfillConflict(require_id(stray.id), 0, p, Y, PersonLink.CORRECTION),
    )
    assert (_codes(database), len(database.state.products)) == (
        [(p, X.scheme, X.store_key, X.code, CodeSource.BACKFILL)],
        1,
    )
    assert second.changes == 0


# --- convergence: every id order, a second apply, a crash after each unit (I23) ----------------

Z = CodeIdentity(CodeScheme.STORE, STORE_KEY, "1111111")
W = CodeIdentity(CodeScheme.STORE, STORE_KEY, "2222222")
OLD_NEW = CodeIdentity(CodeScheme.STORE, STORE_KEY, "3333333")
NAMED_PLU_CSV = (
    "code,name,organic\n3101,Test Apple,0\n4011,Test Banana,0\n4012,Test Orange,0\n"
    "4046,Test Avocado,0\n"
)
type Roles = dict[str, int]
type Expect = Callable[[Roles, FakeDatabase, CatalogBackfill], None]


def _plu_code(code: str) -> CodeIdentity:
    return CodeIdentity(CodeScheme.PLU, "", code)


class CrashError(Exception):
    """The process died right after a unit committed."""


def _lines(database: FakeDatabase) -> list[int | None]:
    return [owner for _, owner in sorted(_owners(database).items())]


def _created(database: FakeDatabase, ids: Roles) -> int:
    (new,) = set(database.state.products) - set(ids.values())
    return new


def _codex(legacy: Legacy, ids: Roles) -> None:
    """C: three X lines; A: two X lines and a person's Y row; B: three Z lines and one Y."""
    legacy.code(ids["a"], Y, CodeSource.MANUAL)
    legacy.buy(*(_item(n, C1, ids["c"]) for n in range(3)))
    legacy.buy(_item(0, C1, ids["a"]), _item(1, C1, ids["a"]))
    legacy.buy(*(_item(n, "1111111 ZED", ids["b"]) for n in range(3)), _item(3, C2, ids["b"]))


def _codex_expected(ids: Roles, database: FakeDatabase, found: CatalogBackfill) -> None:
    """X is C's (more X lines); A's X lines stay (X is A's main code), A attaches nothing and is
    a merge candidate; B's Y line goes to A, Y's owner by its row, and A is still named from its
    main code's line. No product is created."""
    a, b, c = ids["a"], ids["b"], ids["c"]
    assert _codes(database) == sorted(
        [
            (c, X.scheme, X.store_key, X.code, CodeSource.BACKFILL),
            (a, Y.scheme, Y.store_key, Y.code, CodeSource.MANUAL),
            (b, Z.scheme, Z.store_key, Z.code, CodeSource.BACKFILL),
        ]
    )
    assert _lines(database) == [c, c, c, a, a, b, b, b, a]
    assert _product(database, a).canonical_name == "CHOC PISTACH"
    assert (len(database.state.products), found.merge_candidates) == (
        3,
        (MergeCandidate(X, c, a),),
    )


def _mixed(legacy: Legacy, ids: Roles) -> None:
    """P: three X and a Y; Q: two Z, a Y and a W; R: two W."""
    legacy.buy(*(_item(n, C1, ids["p"]) for n in range(3)), _item(3, C2, ids["p"]))
    legacy.buy(
        _item(0, "1111111 ZED", ids["q"]),
        _item(1, "1111111 ZED", ids["q"]),
        _item(2, "1543210 KIWI GOLD", ids["q"]),
        _item(3, "2222222 DUB", ids["q"]),
    )
    legacy.buy(_item(0, "2222222 DUB", ids["r"]), _item(1, "2222222 DUB", ids["r"]))


def _mixed_expected(ids: Roles, database: FakeDatabase, _: CatalogBackfill) -> None:
    """Nobody's main code is Y: one new product for Y takes P's and Q's Y lines, named from P's
    (the earliest); Q's W line goes to R."""
    p, q, r, new = ids["p"], ids["q"], ids["r"], _created(database, ids)
    assert _codes(database) == sorted(
        [
            (p, X.scheme, X.store_key, X.code, CodeSource.BACKFILL),
            (new, Y.scheme, Y.store_key, Y.code, CodeSource.BACKFILL),
            (q, Z.scheme, Z.store_key, Z.code, CodeSource.BACKFILL),
            (r, W.scheme, W.store_key, W.code, CodeSource.BACKFILL),
        ]
    )
    assert _lines(database) == [p, p, p, new, q, q, new, r, r, r]
    assert _product(database, new).canonical_name == "GOLD KIWI"


def _cyclic(legacy: Legacy, ids: Roles) -> None:
    """At Loblaws. A: two 4046 and a 4011, with a person's 3101 row; B: two 4012 and a 3101,
    with a person's 4011 row; B's receipt is the earlier."""
    a, b = ids["a"], ids["b"]
    legacy.code(a, _plu_code("3101"), CodeSource.MANUAL)
    legacy.code(b, _plu_code("4011"), CodeSource.MANUAL)
    legacy.buy(
        _item(0, "4012 TST ORANGE", b),
        _item(1, "4012 TST ORANGE", b),
        _item(2, "3101 TST APPLE", b),
        store=LOBLAWS,
    )
    legacy.buy(
        _item(0, "4046 TST AVOCADO", a),
        _item(1, "4046 TST AVOCADO", a),
        _item(2, "4011 TST BANANA", a),
        store=LOBLAWS,
    )


def _cyclic_expected(ids: Roles, database: FakeDatabase, _: CatalogBackfill) -> None:
    """The lines swap: A's 4011 goes to B and B's 3101 to A. Each is named from its main code
    (A 4046, B 4012), from the PLU table."""
    a, b = ids["a"], ids["b"]
    assert _codes(database) == sorted(
        [
            (a, CodeScheme.PLU, "", "3101", CodeSource.MANUAL),
            (b, CodeScheme.PLU, "", "4011", CodeSource.MANUAL),
            (a, CodeScheme.PLU, "", "4046", CodeSource.BACKFILL),
            (b, CodeScheme.PLU, "", "4012", CodeSource.BACKFILL),
        ]
    )
    assert _lines(database) == [b, b, a, a, a, b]
    assert (_product(database, a).canonical_name, _product(database, b).canonical_name) == (
        "Test Avocado",
        "Test Orange",
    )
    assert len(database.state.products) == 2


def _categorized(legacy: Legacy, ids: Roles) -> None:
    """P: two X and ``3333333 OLD``; Q: two Y and ``3333333 NEW``; CATEGORY corrections for
    ``old`` (produce, t1) and ``new`` (bakery, t2, the newer)."""
    p, q = ids["p"], ids["q"]
    legacy.buy(_item(0, C1, p), _item(1, C1, p), _item(2, "3333333 OLD", p))
    legacy.buy(_item(0, C2, q), _item(1, C2, q), _item(2, "3333333 NEW", q))
    legacy.correct(subject="old", new_value=Category.PRODUCE.value, created_at=at(1))
    legacy.correct(subject="new", new_value=Category.BAKERY.value, created_at=at(2))


def _categorized_expected(ids: Roles, database: FakeDatabase, _: CatalogBackfill) -> None:
    """One created product for 3333333 holds both lines; its category is the newest
    correction among its lines' texts (bakery)."""
    p, q, new = ids["p"], ids["q"], _created(database, ids)
    assert _lines(database) == [p, p, new, q, q, new]
    made = _product(database, new)
    assert (made.canonical_name, made.category, made.field_sources.get("category")) == (
        "OLD",
        Category.BAKERY,
        FieldSource.PERSON,
    )
    assert [row for row in _codes(database) if row[3] == OLD_NEW.code] == [
        (new, OLD_NEW.scheme, OLD_NEW.store_key, OLD_NEW.code, CodeSource.BACKFILL)
    ]


def _foreign_memory(legacy: Legacy, ids: Roles) -> None:
    """A owns X, has one X line and a category a person typed (produce, matching no
    correction for A's own lines); B has a ``memory`` line ``1362394 OTHER``; CATEGORY
    corrections for ``other``: produce at t1, bakery at t2."""
    a, b = ids["a"], ids["b"]
    legacy.code(a, X, CodeSource.MANUAL)

    def typed(uow: UnitOfWork) -> None:
        stored = uow.products.get(a)
        assert stored is not None
        uow.products.update(
            replace(
                stored,
                category=Category.PRODUCE,
                field_sources={**stored.field_sources, "category": FieldSource.PERSON},
            )
        )

    in_unit(legacy.factory, typed)
    legacy.buy(_item(0, C1, a))
    legacy.buy(_item(0, "1362394 OTHER", b, source=MatchSource.MEMORY))
    legacy.correct(subject="other", new_value=Category.PRODUCE.value, created_at=at(1))
    legacy.correct(subject="other", new_value=Category.BAKERY.value, created_at=at(2))


def _foreign_memory_expected(ids: Roles, database: FakeDatabase, found: CatalogBackfill) -> None:
    """B's line is a person's: it stays on B as a conflict and attaches nothing. A's category
    comes from A's own lines only, so B's corrections never touch it; B takes the newest
    correction for its own line."""
    a, b = ids["a"], ids["b"]
    assert _lines(database) == [a, b]
    assert [(c.product_id, c.code, c.reason) for c in found.conflicts] == [
        (b, X, PersonLink.CORRECTION)
    ]
    assert _codes(database) == [(a, X.scheme, X.store_key, X.code, CodeSource.MANUAL)]
    named = _product(database, a)
    assert (named.canonical_name, named.category, named.field_sources["category"]) == (
        "CHOC PISTACH",
        Category.PRODUCE,
        FieldSource.PERSON,
    )
    assert _product(database, b).category == Category.BAKERY


def _multi_code(legacy: Legacy, ids: Roles) -> None:
    """At Loblaws: P holds a person's 4011 row and has one 4046 line and two 4011 lines."""
    p = ids["p"]
    legacy.code(p, _plu_code("4011"), CodeSource.MANUAL)
    legacy.buy(
        _item(0, "4046 TST AVOCADO", p),
        _item(1, "4011 TST BANANA", p),
        _item(2, "4011 TST BANANA", p),
        store=LOBLAWS,
    )


def _multi_code_expected(ids: Roles, database: FakeDatabase, _: CatalogBackfill) -> None:
    """P keeps every line, attaches its main code 4046 and is named from its most frequent
    final code, 4011."""
    p = ids["p"]
    assert _lines(database) == [p, p, p]
    assert _codes(database) == sorted(
        [
            (p, CodeScheme.PLU, "", "4011", CodeSource.MANUAL),
            (p, CodeScheme.PLU, "", "4046", CodeSource.BACKFILL),
        ]
    )
    assert _product(database, p).canonical_name == "Test Banana"


def _overtaken(legacy: Legacy, ids: Roles) -> None:
    """At Loblaws: D holds a person's 4011 row and has one 4046 line; E has two 4012 and a
    4011; F has two 3101 and a 4011."""
    d, e, f = ids["d"], ids["e"], ids["f"]
    legacy.code(d, _plu_code("4011"), CodeSource.MANUAL)
    legacy.buy(_item(0, "4046 TST AVOCADO", d), store=LOBLAWS)
    legacy.buy(
        _item(0, "4012 TST ORANGE", e),
        _item(1, "4012 TST ORANGE", e),
        _item(2, "4011 TST BANANA", e),
        store=LOBLAWS,
    )
    legacy.buy(
        _item(0, "3101 TST APPLE", f),
        _item(1, "3101 TST APPLE", f),
        _item(2, "4011 TST BANANA", f),
        store=LOBLAWS,
    )


def _overtaken_expected(ids: Roles, database: FakeDatabase, _: CatalogBackfill) -> None:
    """Two 4011 lines move into D and outnumber its own 4046 line: D is named from 4011."""
    d, e, f = ids["d"], ids["e"], ids["f"]
    assert _lines(database) == [d, e, e, d, f, f, d]
    assert _codes(database) == sorted(
        [
            (d, CodeScheme.PLU, "", "4011", CodeSource.MANUAL),
            (d, CodeScheme.PLU, "", "4046", CodeSource.BACKFILL),
            (e, CodeScheme.PLU, "", "4012", CodeSource.BACKFILL),
            (f, CodeScheme.PLU, "", "3101", CodeSource.BACKFILL),
        ]
    )
    assert [_product(database, pid).canonical_name for pid in (d, e, f)] == [
        "Test Banana",
        "Test Orange",
        "Test Apple",
    ]


def _empty_destination(legacy: Legacy, ids: Roles) -> None:
    """At Loblaws: A has no ITEM line and holds a person's 4011 row; B has two 4046 and a 4011."""
    a, b = ids["a"], ids["b"]
    legacy.code(a, _plu_code("4011"), CodeSource.MANUAL)
    legacy.buy(
        _item(0, "4046 TST AVOCADO", b),
        _item(1, "4046 TST AVOCADO", b),
        _item(2, "4011 TST BANANA", b),
        store=LOBLAWS,
    )


def _empty_destination_expected(ids: Roles, database: FakeDatabase, _: CatalogBackfill) -> None:
    """B's 4011 line moves to A, which is settled from it in the same run."""
    a, b = ids["a"], ids["b"]
    assert _lines(database) == [b, b, a]
    assert _codes(database) == sorted(
        [
            (a, CodeScheme.PLU, "", "4011", CodeSource.MANUAL),
            (b, CodeScheme.PLU, "", "4046", CodeSource.BACKFILL),
        ]
    )
    assert (_product(database, a).canonical_name, _product(database, b).canonical_name) == (
        "Test Banana",
        "Test Avocado",
    )


def _stale_name(legacy: Legacy, ids: Roles) -> None:
    """Receipts dated D, E, F. D holds a person's 1111111 row and one ``2222222 OLD`` line; E
    has two 3333333 and a ``1111111 NEW``; F has two 4444444 and a ``1111111 NEW``."""
    d, e, f = ids["d"], ids["e"], ids["f"]
    legacy.code(d, CodeIdentity(CodeScheme.STORE, STORE_KEY, "1111111"), CodeSource.MANUAL)
    legacy.buy(_item(0, "2222222 OLD", d))
    legacy.buy(_item(0, "3333333 EEE", e), _item(1, "3333333 EEE", e), _item(2, "1111111 NEW", e))
    legacy.buy(_item(0, "4444444 FFF", f), _item(1, "4444444 FFF", f), _item(2, "1111111 NEW", f))


def _stale_name_expected(ids: Roles, database: FakeDatabase, _: CatalogBackfill) -> None:
    """Both NEW lines move to D; its final lines are OLD and two 1111111 lines, so it is named
    from the earliest 1111111 line, even when an earlier unit had already named it OLD."""
    d, e, f = ids["d"], ids["e"], ids["f"]
    assert _lines(database) == [d, e, e, d, f, f, d]
    assert _codes(database) == sorted(
        (pid, CodeScheme.STORE, STORE_KEY, code, source)
        for pid, code, source in (
            (d, "1111111", CodeSource.MANUAL),
            (d, "2222222", CodeSource.BACKFILL),
            (e, "3333333", CodeSource.BACKFILL),
            (f, "4444444", CodeSource.BACKFILL),
        )
    )
    assert [_product(database, pid).canonical_name for pid in (d, e, f)] == ["NEW", "EEE", "FFF"]


FIXTURES: dict[str, tuple[tuple[str, ...], Callable[[Legacy, Roles], None], Expect]] = {
    "codex": (("a", "b", "c"), _codex, _codex_expected),
    "mixed": (("p", "q", "r"), _mixed, _mixed_expected),
    "cyclic": (("a", "b"), _cyclic, _cyclic_expected),
    "categorized": (("p", "q"), _categorized, _categorized_expected),
    "foreign-memory": (("a", "b"), _foreign_memory, _foreign_memory_expected),
    "multi-code": (("p",), _multi_code, _multi_code_expected),
    "overtaken": (("d", "e", "f"), _overtaken, _overtaken_expected),
    "empty-destination": (("a", "b"), _empty_destination, _empty_destination_expected),
    "stale-name": (("d", "e", "f"), _stale_name, _stale_name_expected),
}
ORDERS = [
    (name, order)
    for name, (roles, _, _) in FIXTURES.items()
    for order in itertools.permutations(roles)
]


@pytest.fixture
def named_plu(tmp_path: Path) -> Path:
    path = tmp_path / "named-plu.csv"
    path.write_text(NAMED_PLU_CSV, encoding="utf-8")
    return path


def _seeded(order: tuple[str, ...], arrange: Callable[[Legacy, Roles], None]) -> FakeDatabase:
    database = FakeDatabase()
    legacy = Legacy(fake_uow_factory(database, FakeClock()))
    ids = {role: legacy.add(legacy_product(canonical_name=f"Legacy {role}")) for role in order}
    arrange(legacy, ids)
    return database


def _roles(database: FakeDatabase) -> Roles:
    return {
        product.canonical_name.removeprefix("Legacy "): pid
        for pid, product in database.state.products.items()
        if product.canonical_name.startswith("Legacy ")
    }


def _final(database: FakeDatabase) -> tuple[object, ...]:
    """Line -> product, code rows and products: the whole outcome."""
    return (_owners(database), _codes(database), database.state.products)


def _run(database: FakeDatabase, plu: Path) -> CatalogBackfill:
    clock = FakeClock()
    return BackfillCatalog(fake_uow_factory(database, clock), clock, PluCatalog(plu))(apply=True)


@pytest.mark.parametrize(("name", "order"), ORDERS, ids=[f"{n}-{''.join(o)}" for n, o in ORDERS])
def test_i23_one_apply_reaches_the_owners_in_every_id_order_and_a_second_changes_nothing(
    named_plu: Path, name: str, order: tuple[str, ...]
) -> None:
    _, arrange, expected = FIXTURES[name]
    database = _seeded(order, arrange)
    ids = _roles(database)

    first = _run(database, named_plu)
    second = _run(database, named_plu)

    expected(ids, database, first)
    assert second.changes == 0


@pytest.mark.parametrize(("name", "order"), ORDERS, ids=[f"{n}-{''.join(o)}" for n, o in ORDERS])
def test_i23_a_crash_after_any_unit_then_a_resume_ends_as_one_apply(
    named_plu: Path, monkeypatch: pytest.MonkeyPatch, name: str, order: tuple[str, ...]
) -> None:
    roles, arrange, _ = FIXTURES[name]
    reference = _seeded(order, arrange)
    _run(reference, named_plu)
    commit = FakeUnitOfWork.commit
    crashes = 0

    for crash_after in itertools.count(1):
        database = _seeded(order, arrange)
        commits = itertools.count(1)

        def crashing(
            self: FakeUnitOfWork,
            at_commit: int = crash_after,
            commits: Iterator[int] = commits,
        ) -> None:
            commit(self)
            if next(commits) == at_commit:
                raise CrashError

        with monkeypatch.context() as patched:
            patched.setattr(FakeUnitOfWork, "commit", crashing)
            try:
                _run(database, named_plu)
            except CatalogBackfillStoppedError:
                crashes += 1
            else:
                break
        _run(database, named_plu)
        assert _final(database) == _final(reference), crash_after
        assert _run(database, named_plu).changes == 0, crash_after

    assert crashes >= len(roles)
