"""Naming a new product from a catalog entry (PR 1a, PC-TC-55) and the ``catalog`` use cases of
PR 1b on the fakes (barcode-lookup plan R15-R17, A1, TQ33, TQ34, TQ37, I9, I13).

Pinned:
- ``entry_product_name(name, brand)`` is ``"<brand> <name>"`` unless the name already holds the
  brand (ignoring case). The line's model-written ``description`` never takes part.
- ``product_code_at(store_key, code)`` reads a bare code with the store's profile;
  DomainValidationError ``{code!r} is not a product code at {store_key}`` otherwise.
  ``parse_size`` takes a positive number and a ``Unit`` value, with or without a space.
- ``CatalogTodo(uow_factory)(*, store=None, limit=None) -> tuple[TodoRow, ...]``: one row per
  stored code of an unconfirmed product with ITEM lines; most lines first, then the newest
  last date (undated last), then the code; counts, last paid and last date at ``store`` only.
- ``AddCatalogCode(uow_factory, clock)``: NAME fills the code's product in place (or creates
  one), stamping ``person``; ``product_id`` adopts the code; a person's name needs ``replace``.
- ``RemoveCatalogCode``: the first stored identity of the bare code; the product is kept.

State TODO: at ``costco`` C1 bought 3 times (last 2026-11-03, 1299 paid after a 400 discount on
a 1699 line), C2 once (2026-11-01, 1599), ``1234567 KS ORG EGGS 24CT`` named by a person, U1's
product without a code; at ``loblaws`` L1 twice (last 2026-11-02, 899), L3 named by PLU.
"""

import copy
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from paper_trail.application.catalog import (
    AddCatalogCode,
    CatalogTodo,
    CodeNamed,
    CodeNaming,
    RemoveCatalogCode,
    TodoRow,
    parse_size,
    product_code_at,
)
from paper_trail.application.payloads import entry_product_name
from paper_trail.domain.enums import (
    Category,
    CodeScheme,
    CodeSource,
    FieldSource,
    MatchSource,
    Unit,
)
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import CodeIdentity, Product, ProductCode
from paper_trail.domain.rules.item_codes import parse_item_code
from paper_trail.ports.search import HitKind
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.agent_harness import build_harness
from tests.extractions import (
    C1,
    C2,
    L1,
    L3,
    LOBLAWS,
    U1,
    WALMART,
    coded_answer,
)
from tests.factories import (
    CREATED_AT,
    MASKED_TEST_PAN,
    STORE_NAME,
    TEST_PAN,
    cad,
    discount_line,
    item_line,
    legacy_product,
    product,
)
from tests.fakes.clock import FakeClock
from tests.fakes.reader import ScriptedReader
from tests.fakes.unit_of_work import FakeDatabase, fake_uow_factory
from tests.storage import in_unit, seed_purchase
from tests.support import require_id

EGGS = "1234567 KS ORG EGGS 24CT"
COSTCO_URL = "https://www.costco.ca/CatalogSearch?keyword={}"
CHOC = CodeIdentity(CodeScheme.STORE, "costco", "1362394")
KIWI = CodeIdentity(CodeScheme.STORE, "costco", "1543210")
EGGS_CODE = CodeIdentity(CodeScheme.STORE, "costco", "1234567")
WIPES = CodeIdentity(CodeScheme.GTIN, "", "0060383064877")
APPLE = CodeIdentity(CodeScheme.PLU, "", "3101")
LATER = timedelta(hours=1)

ENTRY_NAMES = [
    ("Oat Bars", "Acme", "Acme Oat Bars"),
    ("Acme Oat Bars", "Acme", "Acme Oat Bars"),
    ("Acme oat bars", "ACME", "Acme oat bars"),
    ("Test Apple", None, "Test Apple"),
]


@pytest.mark.parametrize(("name", "brand", "expected"), ENTRY_NAMES)
def test_pc_tc_55_a_new_products_name_from_an_entry(
    name: str, brand: str | None, expected: str
) -> None:
    assert entry_product_name(name, brand) == expected


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def factory(database: FakeDatabase, clock: FakeClock) -> UnitOfWorkFactory:
    return fake_uow_factory(database, clock)


@pytest.fixture
def add(factory: UnitOfWorkFactory, clock: FakeClock) -> AddCatalogCode:
    return AddCatalogCode(factory, clock)


@dataclass(frozen=True)
class Todo:
    """Product ids of the TODO state."""

    choc: int
    kiwi: int
    eggs: int
    loose: int
    wipes: int
    apple: int


def _printed(name: str) -> Product:
    return product(
        canonical_name=name,
        brand=None,
        size_value=None,
        size_unit=None,
        category=None,
        tags=(),
        field_sources={"canonical_name": FieldSource.RECEIPT_TEXT},
    )


def _add_product(factory: UnitOfWorkFactory, value: Product) -> int:
    return require_id(in_unit(factory, lambda uow: uow.products.add(value)).id)


def _code(
    factory: UnitOfWorkFactory,
    identity: CodeIdentity,
    product_id: int,
    source: CodeSource = CodeSource.RECEIPT,
) -> ProductCode:
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
    return in_unit(factory, lambda uow: uow.product_codes.upsert(row, replace=True))


def _bought(
    factory: UnitOfWorkFactory,
    store: str,
    day: int | None,
    *lines: tuple[str, int, int],
    discount: int = 0,
) -> None:
    """One receipt at ``store`` on 2026-11-``day`` (undated for None) of (raw text, cents,
    product id) lines, the first one reduced by ``discount`` cents when given."""
    items = [
        item_line(position, text, cents, product_id=pid, match_source=MatchSource.CATALOG)
        for position, (text, cents, pid) in enumerate(lines)
    ]
    if discount:
        items.append(discount_line(len(items), "TPD/1362394", -discount, 0))
    seed_purchase(
        factory,
        items,
        store_name=store,
        purchased_on=None if day is None else date(2026, 11, day),
    )


def _todo(factory: UnitOfWorkFactory) -> Todo:
    ids = Todo(
        choc=_add_product(factory, _printed("CHOC PISTACH")),
        kiwi=_add_product(factory, _printed("GOLD KIWI")),
        eggs=_add_product(
            factory,
            product(
                canonical_name="Kirkland Organic Eggs",
                brand="Kirkland",
                field_sources={"canonical_name": FieldSource.PERSON, "brand": FieldSource.PERSON},
            ),
        ),
        loose=_add_product(factory, _printed("KS ORG EGGS 24CT")),
        wipes=_add_product(factory, _printed("PC UNS BABYWIPES")),
        apple=_add_product(
            factory,
            replace(
                _printed("Test Apple"),
                category=Category.PRODUCE,
                field_sources={"canonical_name": FieldSource.PLU, "category": FieldSource.PLU},
            ),
        ),
    )
    _code(factory, CHOC, ids.choc)
    _code(factory, KIWI, ids.kiwi)
    _code(factory, EGGS_CODE, ids.eggs, CodeSource.MANUAL)
    _code(factory, WIPES, ids.wipes)
    _code(factory, APPLE, ids.apple, CodeSource.PLU)
    _bought(
        factory,
        STORE_NAME,
        1,
        (C1, 1699, ids.choc),
        (C2, 1599, ids.kiwi),
        (EGGS, 899, ids.eggs),
        (U1, 899, ids.loose),
    )
    _bought(factory, STORE_NAME, 2, (C1, 1699, ids.choc))
    _bought(factory, STORE_NAME, 3, (C1, 1699, ids.choc), discount=400)
    _bought(factory, LOBLAWS, 1, (L1, 899, ids.wipes), (L3, 299, ids.apple))
    _bought(factory, LOBLAWS, 2, (L1, 899, ids.wipes))
    return ids


def _row(  # noqa: PLR0913, PLR0917 - one TodoRow
    code: CodeIdentity,
    product_id: int,
    name: str,
    times: int,
    paid: int | None,
    day: int | None,
) -> TodoRow:
    url = COSTCO_URL.format(code.code) if code.store_key == "costco" else None
    return TodoRow(
        code=code,
        product_id=product_id,
        printed_name=name,
        times_bought=times,
        last_paid=None if paid is None else cad(paid),
        last_date=None if day is None else date(2026, 11, day),
        lookup_url=url,
    )


def _todo_rows(ids: Todo) -> list[TodoRow]:
    return [
        _row(CHOC, ids.choc, "CHOC PISTACH", 3, 1299, 3),
        _row(WIPES, ids.wipes, "PC UNS BABYWIPES", 2, 899, 2),
        _row(KIWI, ids.kiwi, "GOLD KIWI", 1, 1599, 1),
    ]


def _codes(factory: UnitOfWorkFactory) -> list[ProductCode]:
    with factory(read_only=True) as uow:
        return list(uow.product_codes.list())


def _product(factory: UnitOfWorkFactory, product_id: int) -> Product:
    with factory(read_only=True) as uow:
        found = uow.products.get(product_id)
    assert found is not None, f"product {product_id} is not stored"
    return found


# --- product_code_at, parse_size --------------------------------------------------------------


@pytest.mark.parametrize(
    ("store_key", "code"),
    [
        ("costco", "12"),
        ("costco", "abc"),
        ("costco", ""),
        ("loblaws", "12"),
        ("loblaws", "51234567890"),
        ("loblaws", "4006381333930"),
        ("loblaws", "123456789"),
        ("loblaws", "1362394 X"),
    ],
)
def test_pc_tc_110_a_code_that_names_no_product_is_refused(store_key: str, code: str) -> None:
    with pytest.raises(DomainValidationError) as refused:
        product_code_at(store_key, code)

    assert str(refused.value) == f"{code!r} is not a product code at {store_key}"


def test_pc_tc_109_a_bare_code_is_read_as_the_code_of_a_line() -> None:
    assert product_code_at("loblaws", "06038306487") == parse_item_code("06038306487 X", "loblaws")


@pytest.mark.parametrize(
    ("text", "size"),
    [
        ("1.36 kg", (Decimal("1.36"), Unit.KG)),
        ("1.36kg", (Decimal("1.36"), Unit.KG)),
        ("500 ml", (Decimal("500"), Unit.ML)),
        ("12 each", (Decimal("12"), Unit.EACH)),
        ("2 LB", (Decimal("2"), Unit.LB)),
    ],
)
def test_pc_tc_114_a_size_is_a_number_and_a_unit(text: str, size: tuple[Decimal, Unit]) -> None:
    assert parse_size(text) == size


@pytest.mark.parametrize("text", ["1.36 stone", "kg", "abc", "-1 kg", "0 kg", ""])
def test_pc_tc_114_anything_else_is_not_a_size(text: str) -> None:
    with pytest.raises(DomainValidationError) as refused:
        parse_size(text)

    assert str(refused.value) == (
        f"size {text!r}: give a positive number and one of each, g, kg, ml, l, lb, oz"
    )


# --- catalog todo (R15) -------------------------------------------------------------------------


def test_pc_tc_103_lists_unconfirmed_coded_products_most_lines_first(
    factory: UnitOfWorkFactory, database: FakeDatabase
) -> None:
    """Not listed: the person-named eggs, the PLU-named apple and the uncoded product. The
    barcode's identity, not its printed digits; no link without a store. Read-only."""
    ids = _todo(factory)
    commits = database.commits

    assert list(CatalogTodo(factory)()) == _todo_rows(ids)
    assert database.commits == commits


@pytest.mark.parametrize(
    ("store", "limit", "picked"),
    [
        ("COSTCO WHOLESALE #1234", None, (0, 2)),
        ("costco", None, (0, 2)),
        ("nowhere", None, ()),
        (None, 1, (0,)),
    ],
)
def test_pc_tc_104_store_and_limit(
    factory: UnitOfWorkFactory, store: str | None, limit: int | None, picked: tuple[int, ...]
) -> None:
    ids = _todo(factory)
    rows = _todo_rows(ids)

    assert list(CatalogTodo(factory)(store=store, limit=limit)) == [rows[i] for i in picked]


def test_pc_tc_103_a_barcode_bought_at_two_stores_is_counted_per_store(
    factory: UnitOfWorkFactory,
) -> None:
    ids = _todo(factory)
    _bought(factory, WALMART, 5, (L1, 999, ids.wipes))
    todo = CatalogTodo(factory)

    assert [todo()[0], *todo(store="loblaws"), *todo(store="walmart")] == [
        _row(WIPES, ids.wipes, "PC UNS BABYWIPES", 3, 999, 5),
        _row(WIPES, ids.wipes, "PC UNS BABYWIPES", 2, 899, 2),
        _row(WIPES, ids.wipes, "PC UNS BABYWIPES", 1, 999, 5),
    ]


def test_pc_tc_103_ties_go_to_the_newest_date_then_the_code_and_undated_last(
    factory: UnitOfWorkFactory,
) -> None:
    """I9: a product with no dated purchase has no last paid or date and sorts last."""
    codes = {
        text: CodeIdentity(CodeScheme.STORE, "costco", text.split()[0])
        for text in ("3333333 A", "2222222 B", "1111111 C", "4444444 D")
    }
    pids = {text: _add_product(factory, _printed(text[8:])) for text in codes}
    for text, code in codes.items():
        _code(factory, code, pids[text])
    _bought(factory, STORE_NAME, 1, ("3333333 A", 100, pids["3333333 A"]))
    _bought(factory, STORE_NAME, 3, ("2222222 B", 200, pids["2222222 B"]))
    _bought(factory, STORE_NAME, 3, ("1111111 C", 300, pids["1111111 C"]))
    _bought(factory, STORE_NAME, None, ("4444444 D", 400, pids["4444444 D"]))

    assert list(CatalogTodo(factory)()) == [
        _row(codes["1111111 C"], pids["1111111 C"], "C", 1, 300, 3),
        _row(codes["2222222 B"], pids["2222222 B"], "B", 1, 200, 3),
        _row(codes["3333333 A"], pids["3333333 A"], "A", 1, 100, 1),
        _row(codes["4444444 D"], pids["4444444 D"], "D", 1, None, None),
    ]


def test_pc_tc_103_tq33_a_product_with_two_codes_is_listed_once_per_code(
    factory: UnitOfWorkFactory,
) -> None:
    ids = _todo(factory)
    second = CodeIdentity(CodeScheme.STORE, "costco", "7777777")
    _code(factory, second, ids.choc)

    assert list(CatalogTodo(factory)(store="costco")) == [
        _row(CHOC, ids.choc, "CHOC PISTACH", 3, 1299, 3),
        _row(second, ids.choc, "CHOC PISTACH", 3, 1299, 3),
        _row(KIWI, ids.kiwi, "GOLD KIWI", 1, 1599, 1),
    ]


def test_pc_tc_156_todo_after_add_product(factory: UnitOfWorkFactory, add: AddCatalogCode) -> None:
    """The old printed-text product lost its code; the adopted eggs are confirmed."""
    ids = _todo(factory)

    add(CodeNaming("costco", "1362394", product_id=ids.eggs))

    assert list(CatalogTodo(factory)()) == _todo_rows(ids)[1:]


# --- catalog add (R16) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("store", "code", "identity"),
    [
        ("costco", "1362394", CHOC),
        ("costco", "3101", CodeIdentity(CodeScheme.STORE, "costco", "3101")),
        ("loblaws", "06038306487", WIPES),
        ("loblaws", "3101", APPLE),
        ("loblaws", "93101", CodeIdentity(CodeScheme.PLU, "", "93101")),
        ("loblaws", "2100580", CodeIdentity(CodeScheme.STORE, "loblaws", "2100580")),
        ("COSTCO WHOLESALE #1234", "1362394", CHOC),
        ("loblaws", "036000291452", CodeIdentity(CodeScheme.GTIN, "", "0036000291452")),
    ],
)
def test_pc_tc_109_the_code_is_parsed_with_the_stores_profile(
    factory: UnitOfWorkFactory, add: AddCatalogCode, store: str, code: str, identity: CodeIdentity
) -> None:
    add(CodeNaming(store, code, name="X"))

    assert _codes(factory) == [
        ProductCode(
            1,
            1,
            identity.scheme,
            identity.store_key,
            identity.code,
            CodeSource.MANUAL,
            CREATED_AT,
            CREATED_AT,
        )
    ]


def test_pc_tc_109_identities_are_tried_in_order(
    factory: UnitOfWorkFactory, add: AddCatalogCode
) -> None:
    second = CodeIdentity(CodeScheme.GTIN, "", "0360002914522")
    pid = _add_product(factory, _printed("SOUP"))
    stored = _code(factory, second, pid)

    named = add(CodeNaming("loblaws", "036000291452", name="X"))

    assert (named.code, named.after.id, _codes(factory)) == (
        second,
        pid,
        [replace(stored, source=CodeSource.MANUAL, updated_at=CREATED_AT)],
    )


def test_pc_tc_111_a_new_code_and_name_create_a_named_product(
    factory: UnitOfWorkFactory, database: FakeDatabase, add: AddCatalogCode, clock: FakeClock
) -> None:
    named = add(
        CodeNaming(
            "costco",
            "1543210",
            name="Gold Kiwi 1 kg",
            brand="Zespri",
            size_value=Decimal("1"),
            size_unit=Unit.KG,
            category=Category.PRODUCE,
        )
    )

    person = FieldSource.PERSON
    created = Product(
        id=1,
        canonical_name="Gold Kiwi 1 kg",
        brand="Zespri",
        size_value=Decimal("1"),
        size_unit=Unit.KG,
        category=Category.PRODUCE,
        tags=(),
        created_at=clock.now(),
        field_sources={
            "canonical_name": person,
            "brand": person,
            "size": person,
            "category": person,
        },
    )
    assert named == CodeNamed(KIWI, CodeSource.MANUAL, None, created, None, 0, changed=True)
    assert list(database.state.products.values()) == [created]
    assert [(row.product_id, row.source) for row in _codes(factory)] == [(1, CodeSource.MANUAL)]


def test_pc_tc_111_a_name_alone_is_the_only_person_field(
    factory: UnitOfWorkFactory, add: AddCatalogCode
) -> None:
    named = add(CodeNaming("costco", "1543210", name="Gold Kiwi"))

    assert (named.after.field_sources, named.after.brand, named.after.category) == (
        {"canonical_name": FieldSource.PERSON},
        None,
        None,
    )


@pytest.mark.parametrize(
    "before",
    [
        _printed("CHOC PISTACH"),
        legacy_product(canonical_name="Chocolate Pistachio Cookies"),
        replace(_printed("Test Choc"), field_sources={"canonical_name": FieldSource.PLU}),
    ],
    ids=["receipt_text", "legacy_model", "plu"],
)
def test_pc_tc_112_a_code_that_names_a_product_fills_it_in_place(
    factory: UnitOfWorkFactory,
    database: FakeDatabase,
    add: AddCatalogCode,
    clock: FakeClock,
    before: Product,
) -> None:
    ids = _todo(factory)
    database.state.products[ids.choc] = replace(before, id=ids.choc)
    snapshot = copy.deepcopy(database.state)
    clock.advance(LATER)

    named = add(CodeNaming("costco", "1362394", name="Choc Pistachio Bars"))

    renamed = replace(
        before,
        id=ids.choc,
        canonical_name="Choc Pistachio Bars",
        field_sources={**before.field_sources, "canonical_name": FieldSource.PERSON},
    )
    (code_id,) = [k for k, row in snapshot.product_codes.items() if row.product_id == ids.choc]
    expected = copy.deepcopy(snapshot)
    expected.products[ids.choc] = renamed
    expected.product_codes[code_id] = replace(
        snapshot.product_codes[code_id], source=CodeSource.MANUAL, updated_at=clock.now()
    )
    assert named == CodeNamed(
        CHOC, CodeSource.MANUAL, replace(before, id=ids.choc), renamed, None, 0, changed=True
    )
    assert database.state == expected
    assert CHOC not in [row.code for row in CatalogTodo(factory)()]


def test_pc_tc_113_a_persons_name_needs_replace(
    factory: UnitOfWorkFactory, database: FakeDatabase, add: AddCatalogCode
) -> None:
    ids = _todo(factory)
    snapshot, commits = copy.deepcopy(database.state), database.commits

    with pytest.raises(ConflictError) as refused:
        add(CodeNaming("costco", "1234567", name="Other Name"))

    assert str(refused.value) == f'product {ids.eggs} "Kirkland Organic Eggs" was named by a person'
    assert (database.state, database.commits) == (snapshot, commits)
    replaced = add(CodeNaming("costco", "1234567", name="Other Name", replace=True))
    assert (replaced.changed, _product(factory, ids.eggs).canonical_name) == (True, "Other Name")


def test_pc_tc_113_the_same_name_again_writes_nothing(
    factory: UnitOfWorkFactory, database: FakeDatabase, add: AddCatalogCode
) -> None:
    ids = _todo(factory)
    snapshot, commits = copy.deepcopy(database.state), database.commits

    named = add(CodeNaming("costco", "1234567", name="Kirkland Organic Eggs"))

    assert named.changed is False
    assert (database.state, database.commits) == (snapshot, commits)
    assert named.after == _product(factory, ids.eggs)


def test_pc_tc_113_replace_guards_the_name_only(
    factory: UnitOfWorkFactory, add: AddCatalogCode
) -> None:
    ids = _todo(factory)

    add(CodeNaming("costco", "1234567", name="Kirkland Organic Eggs", brand="KS"))

    stored = _product(factory, ids.eggs)
    assert (stored.canonical_name, stored.brand, stored.field_sources["brand"]) == (
        "Kirkland Organic Eggs",
        "KS",
        FieldSource.PERSON,
    )


def test_pc_tc_115_product_adopts_the_code_and_leaves_the_old_product(
    factory: UnitOfWorkFactory, database: FakeDatabase, add: AddCatalogCode, clock: FakeClock
) -> None:
    ids = _todo(factory)
    snapshot = copy.deepcopy(database.state)
    clock.advance(LATER)

    named = add(CodeNaming("costco", "1362394", product_id=ids.eggs))

    choc, eggs = snapshot.products[ids.choc], snapshot.products[ids.eggs]
    assert named == CodeNamed(CHOC, CodeSource.ADOPTED, choc, eggs, choc, 3, changed=True)
    (code_id,) = [k for k, row in snapshot.product_codes.items() if row.code == CHOC.code]
    expected = copy.deepcopy(snapshot)
    expected.product_codes[code_id] = replace(
        snapshot.product_codes[code_id],
        product_id=ids.eggs,
        source=CodeSource.ADOPTED,
        updated_at=clock.now(),
    )
    assert database.state == expected


def test_pc_tc_115_an_unknown_code_is_created_for_the_product(
    factory: UnitOfWorkFactory, add: AddCatalogCode, clock: FakeClock
) -> None:
    pid = _add_product(factory, product())

    named = add(CodeNaming("costco", "1543210", product_id=pid))

    stored = _product(factory, pid)
    assert named == CodeNamed(KIWI, CodeSource.ADOPTED, None, stored, None, 0, changed=True)
    assert [(row.product_id, row.source, row.created_at) for row in _codes(factory)] == [
        (pid, CodeSource.ADOPTED, clock.now())
    ]


@pytest.mark.parametrize(
    ("source", "changed"),
    [(CodeSource.ADOPTED, False), (CodeSource.RECEIPT, True)],
    ids=["adopted: nothing to write", "machine: becomes adopted (TQ34)"],
)
def test_pc_tc_115_the_code_already_names_that_product(
    factory: UnitOfWorkFactory,
    database: FakeDatabase,
    clock: FakeClock,
    source: CodeSource,
    changed: bool,
) -> None:
    pid = _add_product(factory, product())
    stored = _code(factory, KIWI, pid, source)
    snapshot, commits = copy.deepcopy(database.state), database.commits
    clock.advance(LATER)

    named = AddCatalogCode(factory, clock)(CodeNaming("costco", "1543210", product_id=pid))

    expected = copy.deepcopy(snapshot)
    if changed:
        expected.product_codes[require_id(stored.id)] = replace(
            stored, source=CodeSource.ADOPTED, updated_at=clock.now()
        )
    assert (named.changed, named.left, database.state) == (changed, None, expected)
    assert database.commits == commits + (1 if changed else 0)


def test_pc_tc_115_an_unknown_product_is_not_found(
    factory: UnitOfWorkFactory, database: FakeDatabase, add: AddCatalogCode
) -> None:
    _todo(factory)
    snapshot = copy.deepcopy(database.state)

    with pytest.raises(NotFoundError, match=r"^product 999 does not exist$"):
        add(CodeNaming("costco", "1362394", product_id=999))

    assert database.state == snapshot


def test_pc_tc_151_a_store_code_never_touches_another_store(
    factory: UnitOfWorkFactory, database: FakeDatabase, add: AddCatalogCode
) -> None:
    ids = _todo(factory)
    snapshot = copy.deepcopy(database.state)

    named = add(CodeNaming("loblaws", "1362394", name="X"))

    new_id = require_id(named.after.id)
    assert new_id not in snapshot.products
    assert database.state.products[ids.choc] == snapshot.products[ids.choc]
    assert [(r.scheme, r.store_key, r.code, r.product_id) for r in _codes(factory)][-1] == (
        CodeScheme.STORE,
        "loblaws",
        "1362394",
        new_id,
    )
    assert [row for row in _codes(factory) if row.store_key == "costco"] == [
        row for row in snapshot.product_codes.values() if row.store_key == "costco"
    ]


@pytest.mark.parametrize(
    ("code", "name", "line", "same"),
    [
        ("06038306487", "PC Unscented Baby Wipes", L1, True),
        ("3101", "Test Apple", L3, True),
        ("2100580", "Chicken Breast", "2100580 CHKN BRST MRJ", False),
    ],
    ids=["barcode", "plu", "in-store code"],
)
def test_pc_tc_152_a_barcode_or_plu_is_global_a_store_code_is_its_stores(
    code: str, name: str, line: str, same: bool
) -> None:
    harness = build_harness(ScriptedReader(coded_answer([line], store_name=WALMART)))
    named = AddCatalogCode(harness.uow_factory, harness.clock)(
        CodeNaming("loblaws", code, name=name)
    )

    outcome = harness.process_image("walmart")

    (linked,) = harness.receipt(outcome.receipt_id).items
    source = MatchSource.CATALOG if same else MatchSource.NEW
    assert (linked.match_source, linked.product_id == named.after.id) == (source, same)


def test_ce_tc_036_naming_a_receipts_product_embeds_nothing_and_fts_finds_the_new_name() -> None:
    """CE-TC-036 (V1, D5): a product a receipt created, named with ``catalog add``, is stored
    and found by text search under its new name; there is no vector index to write to."""
    harness = build_harness(ScriptedReader(coded_answer([C1], store_name=STORE_NAME)))
    receipt_id = harness.process_image("costco").receipt_id
    created = require_id(harness.line(receipt_id, 0).product_id)

    named = AddCatalogCode(harness.uow_factory, harness.clock)(
        CodeNaming("costco", "1362394", name="Chocolate Pistachio Bars")
    )

    assert named.after.id == created
    assert harness.product(created).canonical_name == "Chocolate Pistachio Bars"
    with harness.uow_factory(read_only=True) as uow:
        assert not hasattr(uow, "vectors")
        hits = uow.text_search.search("chocolate pistachio bars", limit=10)
    assert [(hit.kind, hit.id) for hit in hits] == [(HitKind.PRODUCT, created)]


@pytest.mark.parametrize(("name", "brand"), [(f"Gift {TEST_PAN}", None), ("Gift", TEST_PAN)])
def test_pc_tc_154_add_masks_free_text(add: AddCatalogCode, name: str, brand: str | None) -> None:
    named = add(CodeNaming("costco", "1543210", name=name, brand=brand))

    assert (named.after.canonical_name, named.after.brand) == (
        name.replace(TEST_PAN, MASKED_TEST_PAN),
        None if brand is None else MASKED_TEST_PAN,
    )


# --- catalog remove (R17) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("store", "code", "removed"),
    [("costco", "1362394", CHOC), ("loblaws", "06038306487", WIPES)],
)
def test_pc_tc_120_remove_deletes_the_mapping_and_keeps_the_product(
    factory: UnitOfWorkFactory,
    database: FakeDatabase,
    store: str,
    code: str,
    removed: CodeIdentity,
) -> None:
    _todo(factory)
    snapshot = copy.deepcopy(database.state)
    (row_id,) = [
        k
        for k, row in snapshot.product_codes.items()
        if (row.scheme, row.store_key, row.code)
        == (removed.scheme, removed.store_key, removed.code)
    ]

    gone = RemoveCatalogCode(factory)(store, code)

    expected = copy.deepcopy(snapshot)
    del expected.product_codes[row_id]
    assert (gone, database.state) == (snapshot.product_codes[row_id], expected)


@pytest.mark.parametrize("stored", ["0036000291452", "0360002914522"])
def test_pc_tc_120_remove_finds_whichever_identity_is_stored(
    factory: UnitOfWorkFactory, stored: str
) -> None:
    pid = _add_product(factory, _printed("SOUP"))
    row = _code(factory, CodeIdentity(CodeScheme.GTIN, "", stored), pid)

    assert RemoveCatalogCode(factory)("loblaws", "036000291452") == row
    assert _codes(factory) == []


@pytest.mark.parametrize(
    ("code", "error"),
    [("1543210", NotFoundError), ("12", DomainValidationError)],
    ids=["unknown mapping", "unparsable"],
)
def test_pc_tc_120_remove_refuses_and_changes_nothing(
    factory: UnitOfWorkFactory, database: FakeDatabase, code: str, error: type[Exception]
) -> None:
    pid = _add_product(factory, _printed("CHOC PISTACH"))
    _code(factory, CHOC, pid)
    snapshot = copy.deepcopy(database.state)

    with pytest.raises(error):
        RemoveCatalogCode(factory)("costco", code)

    assert database.state == snapshot


def test_pc_tc_120_unknown_mapping_names_the_code(factory: UnitOfWorkFactory) -> None:
    with pytest.raises(NotFoundError, match=r"^no product code store costco 1543210$"):
        RemoveCatalogCode(factory)("costco", "1543210")


def test_pc_tc_155_remove_then_add_again(factory: UnitOfWorkFactory, add: AddCatalogCode) -> None:
    ids = _todo(factory)
    RemoveCatalogCode(factory)("costco", "1362394")

    add(CodeNaming("costco", "1362394", product_id=ids.choc))

    assert [(row.product_id, row.source) for row in _codes(factory) if row.code == CHOC.code] == [
        (ids.choc, CodeSource.ADOPTED)
    ]
