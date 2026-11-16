"""``catalog backfill`` on real SQLite: idempotence, integrity, a failure inside a group, a race
with a correction, and re-normalization with code learning under interruption (barcode-lookup
plan R18, A3, A4, A7, A8, R11, R12; PC-TC-139, 140, 141, 143, 159, 160, 163), at head without
the product vectors (claude-extraction CE-TC-054).

Pinned: a second ``apply`` writes nothing (every ``updated_at`` included); a failure inside a
group leaves that group (source, destination, codes, lines, observations, aliases,
``receipts.updated_at``) exactly as before, earlier groups applied, and a rerun converges to the
uninterrupted result; membership is read inside the group's unit; the backfill opens no socket
and does not depend on ``CATALOG__ENABLED``. Failures are injected by patching the SQLite
repository method named in each row, failing after it wrote.
"""

import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from paper_trail.adapters.catalog.plu import PluCatalog
from paper_trail.adapters.sqlite.correction_repository import SqliteCorrectionRepository
from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.price_repository import SqlitePriceRepository
from paper_trail.adapters.sqlite.product_code_repository import SqliteProductCodeRepository
from paper_trail.adapters.sqlite.product_repository import SqliteProductRepository
from paper_trail.adapters.sqlite.receipt_repository import SqliteReceiptRepository
from paper_trail.adapters.sqlite.unit_of_work import SqliteUnitOfWork
from paper_trail.application.catalog import (
    BackfillCatalog,
    CatalogBackfillStoppedError,
    CodeNaming,
)
from paper_trail.application.corrections import CorrectLineItem
from paper_trail.bootstrap import Container, build_container, dispose
from paper_trail.config import CatalogSettings, Settings
from paper_trail.domain.enums import (
    AliasSource,
    CodeScheme,
    CodeSource,
    CorrectionKind,
    FieldSource,
    LineKind,
    MatchSource,
)
from paper_trail.domain.models import LineItem, Product, ProductCode, Receipt
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.extractions import C1, C2, L1, L3, LOBLAWS, PLU_CSV
from tests.factories import (
    CREATED_AT,
    STORE_NAME,
    at,
    correction,
    discount_line,
    item_line,
    legacy_product,
    product,
    product_alias,
)
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, seed_purchase, sqlite_uow_factory
from tests.support import CopyMigrated, MakeSettings, require_id

EGGS = "1234567 KS ORG EGGS 24CT"
TABLES = (
    "receipts",
    "products",
    "product_codes",
    "product_aliases",
    "line_items",
    "price_observations",
    "corrections",
)
WRITTEN_AT = {"receipts": ("updated_at",), "products": ("created_at",)}

type Rows = dict[str, list[dict[str, Any]]]


@dataclass
class Db:
    engine: Engine
    factory: UnitOfWorkFactory
    clock: FakeClock

    def add(self, stored: Product) -> int:
        return require_id(in_unit(self.factory, lambda uow: uow.products.add(stored)).id)

    def buy(self, *lines: LineItem, store: str = STORE_NAME, day: int = 0) -> Receipt:
        stored = seed_purchase(
            self.factory, lines, store_name=store, purchased_on=date(2026, 11, 1 + day)
        )

        def remember(uow: UnitOfWork) -> None:
            for item in stored.items:
                key = normalize_alias(item.raw_text)
                if (
                    item.product_id is not None
                    and item.kind is LineKind.ITEM
                    and uow.products.alias_at(key, stored.store_key) is None
                ):
                    uow.products.add_alias(
                        product_alias(
                            item.product_id,
                            alias=key,
                            store_key=stored.store_key,
                            source=AliasSource.MATCHED,
                        )
                    )

        in_unit(self.factory, remember)
        return stored

    def store(self, action: Callable[[UnitOfWork], object]) -> None:
        in_unit(self.factory, action)

    def backfill(self, plu: PluCatalog, *, apply: bool = True) -> Any:
        return BackfillCatalog(self.factory, self.clock, plu)(apply=apply)

    def snapshot(self) -> Rows:
        with self.engine.connect() as connection:
            return {
                table: [
                    dict(row)
                    for row in connection.exec_driver_sql(
                        f'SELECT * FROM "{table}" ORDER BY id'  # noqa: S608 - fixed names
                    ).mappings()
                ]
                for table in TABLES
            }


def _item(
    position: int,
    raw_text: str,
    product_id: int,
    cents: int = 899,
    source: MatchSource = MatchSource.NEW,
) -> LineItem:
    return item_line(position, raw_text, cents, product_id=product_id, match_source=source)


def _comparable(rows: Rows) -> Rows:
    """Rows without the timestamps a rerun writes at its own time."""
    dropped = {"updated_at", "created_at"}
    return {
        table: [
            {key: value for key, value in row.items() if key not in dropped}
            if table in {"receipts", "products", "product_codes"}
            else row
            for row in table_rows
        ]
        for table, table_rows in rows.items()
    }


@pytest.fixture
def plu(tmp_path: Path) -> PluCatalog:
    path = tmp_path / "plu.csv"
    path.write_text(PLU_CSV, encoding="utf-8")
    return PluCatalog(path)


@pytest.fixture
def open_db(tmp_path: Path, copy_migrated: CopyMigrated) -> Iterator[Callable[[str], Db]]:
    engines: list[Engine] = []

    def open_one(name: str) -> Db:
        engine = create_engine(copy_migrated(tmp_path / name / "paper_trail.db"))
        engines.append(engine)
        clock = FakeClock()
        return Db(engine, sqlite_uow_factory(engine, clock), clock)

    yield open_one
    for engine in engines:
        engine.dispose()


def _rich(db: Db) -> None:
    """126 (one code), 128 (split, discount, uncoded), 133 a (a person's name), 134 (PLU),
    136 (an IMPORT alias), 137 (an old correction key), and a product whose two stray lines of
    one new code (``8888888``) have different texts, a person's CATEGORY correction being for
    the second (a created product described from every line moved into it)."""
    wipes = db.add(legacy_product(canonical_name="Pampers Baby Wipes"))
    choc = db.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    apple = db.add(legacy_product(canonical_name="Apples"))
    eggs = db.add(legacy_product(canonical_name="Organic Eggs"))
    db.buy(_item(0, L1, wipes), _item(1, L1, wipes), store=LOBLAWS)
    db.buy(
        _item(0, C1, choc),
        _item(1, C1, choc),
        _item(2, C2, choc, 1599),
        discount_line(3, "TPD/1543210", -400, 2),
        _item(4, "KS ORG EGGS", choc),
        day=1,
    )
    db.buy(_item(0, L3, apple), store=LOBLAWS, day=2)
    bought = db.buy(_item(0, EGGS, eggs), day=3)
    mixed = db.add(legacy_product(canonical_name="Main Thing"))
    db.buy(
        _item(0, "7777777 MAIN THING", mixed),
        _item(1, "7777777 MAIN THING", mixed),
        _item(2, "8888888 KIWI A", mixed),
        _item(3, "8888888 KIWI B", mixed),
        day=4,
    )
    db.store(
        lambda uow: (
            uow.products.add_alias(product_alias(choc, alias="pistachio")),
            uow.corrections.add(
                correction(
                    kind=CorrectionKind.PRODUCT_LINK,
                    subject="ks org eggs 24ct",
                    new_value=str(eggs),
                    line_item_id=bought.items[0].id,
                )
            ),
            uow.corrections.add(correction(subject="tst apple mrj", store_key="loblaws")),
            uow.corrections.add(correction(subject="kiwi b", new_value="produce")),
        )
    )


def test_pc_tc_139_a_second_apply_writes_nothing(
    open_db: Callable[[str], Db], plu: PluCatalog
) -> None:
    db = open_db("idempotent")
    _rich(db)
    first = db.backfill(plu)
    db.clock.advance(timedelta(hours=1))
    before = db.snapshot()

    second = db.backfill(plu)
    dry = db.backfill(plu, apply=False)

    with db.factory(read_only=True) as uow:
        kiwi = uow.product_codes.find(CodeScheme.STORE, "costco", "8888888")
        made = None if kiwi is None else uow.products.get(kiwi.product_id)
    assert made is not None
    assert (made.canonical_name, made.category) == ("KIWI A", "produce")
    assert first.changes > 0
    assert (second.changes, dry.changes, db.snapshot()) == (0, 0, before)


def test_i6_a_dry_run_on_a_populated_database_writes_nothing_and_reports_the_apply(
    open_db: Callable[[str], Db], plu: PluCatalog
) -> None:
    """Every table is unchanged by the dry run, and its report is the following apply's, line
    for line."""
    db = open_db("dry-run")
    _rich(db)
    before = db.snapshot()

    dry = db.backfill(plu, apply=False)
    after_dry = db.snapshot()
    applied = db.backfill(plu)

    assert after_dry == before
    assert dry.products
    assert (dry.applied, applied.applied) == (False, True)
    assert replace(dry, applied=True) == applied


def test_pc_tc_143_integrity_after_apply(open_db: Callable[[str], Db], plu: PluCatalog) -> None:
    db = open_db("integrity")
    _rich(db)
    db.backfill(plu)

    with db.engine.connect() as connection:
        broken = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        misfiled = connection.execute(
            text(
                "SELECT l.id FROM line_items l JOIN price_observations o ON o.line_item_id = l.id"
                " WHERE l.kind = 'item' AND o.product_id IS NOT l.product_id"
            )
        ).all()
        views = connection.execute(
            text(
                "SELECT p.id,"
                " (SELECT count(*) FROM line_items l WHERE l.product_id = p.id"
                "  AND l.kind = 'item'),"
                " (SELECT count(*) FROM price_observations o WHERE o.product_id = p.id)"
                " FROM products p"
            )
        ).all()
        duplicates = connection.execute(
            text(
                "SELECT scheme, store_key, code FROM product_codes"
                " GROUP BY scheme, store_key, code HAVING count(*) > 1"
            )
        ).all()
    assert (broken, misfiled, duplicates) == ([], [], [])
    assert [(pid, lines) for pid, lines, _ in views] == [(pid, seen) for pid, _, seen in views]


@dataclass(frozen=True)
class Group:
    source: int
    destination: int | None
    receipt_id: int
    stray_line_id: int


def _three_groups(db: Db, *, existing: bool) -> Group:
    """Group 1: wipes (L1); group 2: X, X, Y, its Y line moving to a new product or to D (a
    ``legacy_model`` product that already holds Y); group 3: eggs."""
    wipes = db.add(legacy_product(canonical_name="Pampers Baby Wipes"))
    choc = db.add(legacy_product(canonical_name="Chocolate Pistachio Bars"))
    eggs = db.add(legacy_product(canonical_name="Organic Eggs"))
    destination = None
    if existing:
        destination = db.add(legacy_product(canonical_name="Kiwi"))
        code = ProductCode(
            None,
            destination,
            CodeScheme.STORE,
            "costco",
            "1543210",
            CodeSource.RECEIPT,
            CREATED_AT,
            CREATED_AT,
        )
        db.store(lambda uow: uow.product_codes.upsert(code, replace=False))
    db.buy(_item(0, L1, wipes), store=LOBLAWS)
    split = db.buy(_item(0, C1, choc), _item(1, C1, choc), _item(2, C2, choc, 1599), day=1)
    db.buy(_item(0, EGGS, eggs), day=2)
    return Group(choc, destination, require_id(split.id), require_id(split.items[2].id))


def _fail_after(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str, when: Callable[..., bool]
) -> None:
    original = getattr(owner, name)

    def failing(self: object, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        if when(*args, **kwargs):
            msg = "disk I/O error"
            raise RuntimeError(msg)
        return result

    monkeypatch.setattr(owner, name, failing)


type Fault = tuple[type, str, Callable[[Group], Callable[..., bool]]]
FAULTS: dict[str, Fault] = {
    "destination-created": (SqliteProductRepository, "add", lambda g: lambda *_: True),
    "destination-reset": (
        SqliteProductRepository, "update", lambda g: lambda p, *_: p.id == g.destination,
    ),
    "code": (
        SqliteProductCodeRepository, "upsert", lambda g: lambda c, *_, **__: c.code == "1543210",
    ),
    "line-move": (
        SqliteReceiptRepository, "link_items", lambda g: lambda rid, *_: rid == g.receipt_id,
    ),
    "price-reassign": (
        SqlitePriceRepository, "reassign", lambda g: lambda lid, *_: lid == g.stray_line_id,
    ),
    "alias": (SqliteProductRepository, "update_alias", lambda g: lambda *_: True),
}  # fmt: skip
CASES = [
    *[("new", fault) for fault in FAULTS if fault != "destination-reset"],
    *[("existing", fault) for fault in FAULTS if fault not in {"destination-created", "code"}],
]


@pytest.mark.parametrize(("destination", "fault"), CASES, ids=[f"{d}-{f}" for d, f in CASES])
def test_pc_tc_140_a_failing_group_rolls_back_whole_and_a_rerun_finishes(
    open_db: Callable[[str], Db],
    plu: PluCatalog,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
    fault: str,
) -> None:
    reference, db = open_db("reference"), open_db("faulty")
    _three_groups(reference, existing=destination == "existing")
    reference.backfill(plu)
    group = _three_groups(db, existing=destination == "existing")
    before = db.snapshot()
    owner, name, when = FAULTS[fault]

    with monkeypatch.context() as patched:
        _fail_after(patched, owner, name, when(group))
        with pytest.raises(CatalogBackfillStoppedError) as stopped:
            db.backfill(plu)

    done = reference.snapshot()
    expected = {
        **before,
        "products": [done["products"][0], *before["products"][1:]],
        "product_codes": sorted(
            before["product_codes"]
            + [row for row in done["product_codes"] if row["product_id"] == 1],
            key=lambda row: row["id"],
        ),
    }
    assert stopped.value.step == f"product {group.source}"
    assert db.snapshot() == expected
    db.clock.advance(timedelta(hours=1))
    db.backfill(plu)
    assert _comparable(db.snapshot()) == _comparable(done)


def test_pc_tc_141_a_line_corrected_before_its_group_is_left_where_the_person_put_it(
    open_db: Callable[[str], Db], plu: PluCatalog
) -> None:
    """The race runs when the first unit after group 1's commit has closed: a subclass of the
    SQLite unit of work whose ``__exit__`` relinks the stray Y line in its own unit."""
    db = open_db("race")
    group = _three_groups(db, existing=False)
    person = db.add(product(field_sources={"canonical_name": FieldSource.PERSON}))
    raced: list[bool] = []

    def race() -> None:
        with db.factory(read_only=True) as uow:
            group_one_done = bool(uow.product_codes.for_product(1))
        if group_one_done and not raced:
            raced.append(True)
            CorrectLineItem(db.factory, db.clock)(group.receipt_id, 2, product_id=person)

    class Racing(SqliteUnitOfWork):
        def __exit__(self, *exc: object) -> None:
            super().__exit__(*exc)
            race()

    def racing(*, read_only: bool = False) -> SqliteUnitOfWork:
        return Racing(db.engine, db.clock, read_only=read_only)

    found = BackfillCatalog(racing, db.clock, plu)(apply=True)

    with db.factory(read_only=True) as uow:
        receipt = uow.receipts.get(group.receipt_id)
    assert raced == [True]
    assert receipt is not None
    assert (receipt.items[2].product_id, receipt.items[2].match_source) == (
        person,
        MatchSource.MEMORY,
    )
    assert (found.splits, found.conflicts) == ((), ())


def _corrected_twice(db: Db) -> tuple[int, int]:
    """L1's code corrected through two aliases: ``pc unsc babywipes`` -> A at t2 and the old key
    ``pc uns babywipes hmrj`` -> B at t1, plus ``pc uns babywipes`` -> B at t0, B's creating
    correction, which collides with the t1 row after re-normalization (both are kept)."""
    t0, t1, t2 = at(-30), at(-20), at(-10)
    a = db.add(legacy_product(canonical_name="Other Wipes", created_at=at(-120)))
    b = db.add(legacy_product(canonical_name="My Wipes", created_at=t0))
    bought = db.buy(
        _item(0, "06038306487 PC UNSC BABYWIPES", a, source=MatchSource.MEMORY),
        _item(1, L1, b, source=MatchSource.MEMORY),
        _item(2, L1, b, source=MatchSource.MEMORY),
        store=LOBLAWS,
    )
    rows = (
        (0, "pc unsc babywipes", a, t2),
        (1, "pc uns babywipes hmrj", b, t1),
        (2, "pc uns babywipes", b, t0),
    )
    db.store(
        lambda uow: [
            uow.corrections.add(
                correction(
                    kind=CorrectionKind.PRODUCT_LINK,
                    subject=subject,
                    new_value=str(target),
                    store_key="loblaws",
                    receipt_id=bought.id,
                    line_item_id=bought.items[position].id,
                    created_at=created_at,
                )
            )
            for position, subject, target, created_at in rows
        ]
    )
    return a, b


INTERRUPTIONS: dict[str, Fault] = {
    "after-first-rewrite": (SqliteCorrectionRepository, "update", lambda _: lambda *__: True),
    "in-a-product-group": (SqliteProductRepository, "update", lambda _: lambda *__: True),
    "after-first-code-learned": (
        SqliteProductCodeRepository, "upsert",
        lambda _: lambda c, *__, **___: c.source is CodeSource.CORRECTION,
    ),
}  # fmt: skip


@pytest.mark.parametrize("fault", [None, *INTERRUPTIONS])
def test_pc_tc_163_learning_and_key_migration_survive_an_interruption(
    open_db: Callable[[str], Db],
    plu: PluCatalog,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    reference, db = open_db("reference"), open_db("faulty")
    _corrected_twice(reference)
    reference.backfill(plu)
    a, b = _corrected_twice(db)

    if fault is not None:
        owner, name, when = INTERRUPTIONS[fault]
        with monkeypatch.context() as patched:
            _fail_after(patched, owner, name, when(Group(0, None, 0, 0)))
            with pytest.raises(CatalogBackfillStoppedError):
                db.backfill(plu)
        db.clock.advance(timedelta(hours=1))
    db.backfill(plu)

    with db.factory(read_only=True) as uow:
        code = uow.product_codes.find(CodeScheme.GTIN, "", "0060383064877")
        named = uow.products.get(b)
        subjects = sorted(c.subject for c in uow.corrections.list(CorrectionKind.PRODUCT_LINK))
    assert code is not None
    assert named is not None
    assert (code.product_id, code.source, subjects) == (
        a,
        CodeSource.CORRECTION,
        ["pc uns babywipes", "pc uns babywipes", "pc unsc babywipes"],
    )
    assert (named.canonical_name, named.field_sources["canonical_name"]) == (
        "My Wipes",
        FieldSource.PERSON,
    )
    assert _comparable(db.snapshot()) == _comparable(reference.snapshot())


def _seeded_container(settings: Settings, copy_migrated: CopyMigrated) -> Container:
    """A container on a migrated database holding ``_rich``'s legacy state."""
    engine = create_engine(copy_migrated(settings.database_path))
    try:
        clock = FakeClock()
        _rich(Db(engine, sqlite_uow_factory(engine, clock), clock))
    finally:
        engine.dispose()
    return build_container(settings)


def test_pc_tc_159_the_backfill_and_add_do_not_depend_on_catalog_enabled(
    make_settings: MakeSettings, copy_migrated: CopyMigrated
) -> None:
    container = _seeded_container(
        make_settings(catalog=CatalogSettings(enabled=False)), copy_migrated
    )
    try:
        container.backfill_catalog(apply=True)
        named = container.add_catalog_code(CodeNaming(store="costco", code="1543210", name="Kiwi"))
        with container.uow_factory(read_only=True) as uow:
            wipes = uow.products.get(1)
            kiwi = uow.product_codes.find(CodeScheme.STORE, "costco", "1543210")
    finally:
        dispose(container)
    assert wipes is not None
    assert kiwi is not None
    assert (wipes.canonical_name, dict(wipes.field_sources)) == (
        "PC UNS BABYWIPES",
        {"canonical_name": FieldSource.RECEIPT_TEXT},
    )
    assert (kiwi.product_id, kiwi.source) == (require_id(named.after.id), CodeSource.MANUAL)


def test_pc_tc_160_the_backfill_opens_no_connection(
    make_settings: MakeSettings, copy_migrated: CopyMigrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container has no model or embedding client for the backfill to reach; any socket
    (an Ollama host included) fails the run."""
    container = _seeded_container(make_settings(), copy_migrated)

    def refuse(*_: object, **__: object) -> None:
        msg = "network access attempted"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    try:
        found = container.backfill_catalog(apply=True)
    finally:
        dispose(container)
    assert (found.applied, found.changes > 0) == (True, True)


def _milk(db: Db) -> dict[str, int]:
    """Two PRODUCT_LINK corrections whose subjects collide after re-normalization (``milk
    hmrj``, ``milk``) on lines of different Costco codes, both naming P."""
    p = db.add(legacy_product(canonical_name="Milk"))
    bought = db.buy(
        _item(0, "1111 MILK HMRJ", p, source=MatchSource.MEMORY),
        _item(1, "2222 MILK", p, source=MatchSource.MEMORY),
    )
    db.store(
        lambda uow: [
            uow.corrections.add(
                correction(
                    kind=CorrectionKind.PRODUCT_LINK,
                    subject=subject,
                    new_value=str(p),
                    receipt_id=bought.id,
                    line_item_id=bought.items[position].id,
                    created_at=at(position),
                )
            )
            for position, subject in enumerate(("milk hmrj", "milk"))
        ]
    )
    return {"1111": p, "2222": p}


def _pears(db: Db) -> dict[str, int]:
    """``3102 TST PEAR MRJ`` corrected to product 2 (old key ``tst pear mrj``, t1) and ``93102
    TST PEAR`` to product 3 (``tst pear``, t2) at Loblaws; product 2 also has two ``4011``
    lines."""
    conventional = db.add(legacy_product(canonical_name="Pears"))
    organic = db.add(legacy_product(canonical_name="Organic Pears"))
    bought = db.buy(
        _item(0, "3102 TST PEAR MRJ", conventional, source=MatchSource.MEMORY),
        _item(1, "4011 BANANA", conventional),
        _item(2, "4011 BANANA", conventional),
        _item(3, "93102 TST PEAR", organic, source=MatchSource.MEMORY),
        store=LOBLAWS,
    )
    db.store(
        lambda uow: [
            uow.corrections.add(
                correction(
                    kind=CorrectionKind.PRODUCT_LINK,
                    subject=subject,
                    new_value=str(target),
                    store_key="loblaws",
                    receipt_id=bought.id,
                    line_item_id=bought.items[position].id,
                    created_at=at(minutes),
                )
            )
            for position, subject, target, minutes in (
                (0, "tst pear mrj", conventional, 1),
                (3, "tst pear", organic, 2),
            )
        ]
    )
    return {"3102": conventional, "93102": organic}


@pytest.mark.parametrize("arrange", [_milk, _pears], ids=["costco-1111-2222", "plu-3102-93102"])
def test_pc_tc_163_colliding_corrections_on_different_codes_both_teach_their_code(
    open_db: Callable[[str], Db], plu: PluCatalog, arrange: Callable[[Db], dict[str, int]]
) -> None:
    db = open_db("collide")
    expected = arrange(db)

    db.backfill(plu)

    with db.factory(read_only=True) as uow:
        learned = {
            row.code: row.product_id
            for row in uow.product_codes.list()
            if row.source is CodeSource.CORRECTION
        }
    assert learned == expected


def test_i19_a_line_whose_import_alias_loses_its_key_keeps_its_link_as_memory(
    open_db: Callable[[str], Db], plu: PluCatalog
) -> None:
    db = open_db("i19")
    p = db.add(legacy_product(canonical_name="My Chosen Name"))
    q = db.add(legacy_product(canonical_name="Something Else"))
    db.buy(_item(0, C1, p), _item(1, C1, p))
    stray = db.buy(_item(0, "1543210 STRAY HMRJ", p), day=1)
    db.store(
        lambda uow: (
            uow.products.add_alias(
                product_alias(q, alias="stray", source=AliasSource.IMPORT, created_at=at(-1))
            ),
            uow.products.add_alias(
                product_alias(p, alias="stray hmrj", source=AliasSource.IMPORT, created_at=at(-2))
            ),
        )
    )
    rid = require_id(stray.id)

    db.backfill(plu)
    db.clock.advance(timedelta(hours=1))
    second = db.backfill(plu)

    with db.factory(read_only=True) as uow:
        stored = uow.receipts.get(rid)
        losing = uow.products.alias_at("stray hmrj", "costco")
    assert stored is not None
    assert (stored.items[0].product_id, stored.items[0].match_source) == (p, MatchSource.MEMORY)
    assert (losing, second.changes) == (None, 0)


def _kept_code_state(db: Db, *, owner_first: bool) -> tuple[int, int]:
    """A has two ``1111111`` lines and a mis-linked ``2222222 GOLD KIWI 2LB``; B has three
    ``2222222 KIWI GOLD`` lines. ``owner_first`` gives B the lower id. Returns (A, B)."""
    if owner_first:
        b = db.add(legacy_product(canonical_name="Gold Kiwi"))
        a = db.add(legacy_product(canonical_name="Thing"))
    else:
        a = db.add(legacy_product(canonical_name="Thing"))
        b = db.add(legacy_product(canonical_name="Gold Kiwi"))
    db.buy(*(_item(n, "2222222 KIWI GOLD", b) for n in range(3)))
    db.buy(
        _item(0, "1111111 THING", a),
        _item(1, "1111111 THING", a),
        _item(2, "2222222 GOLD KIWI 2LB", a),
        day=1,
    )
    return a, b


@pytest.mark.parametrize("owner_first", [False, True], ids=["owner-higher-id", "owner-lower-id"])
def test_i21_a_moved_line_goes_to_the_product_that_keeps_its_code(
    open_db: Callable[[str], Db], plu: PluCatalog, owner_first: bool
) -> None:
    db = open_db("kept-code")
    a, b = _kept_code_state(db, owner_first=owner_first)

    found = db.backfill(plu)

    with db.engine.connect() as connection:
        codes = connection.exec_driver_sql(
            "SELECT code, product_id, source FROM product_codes ORDER BY code"
        ).all()
        stray = connection.exec_driver_sql(
            "SELECT product_id FROM line_items WHERE raw_text = '2222222 GOLD KIWI 2LB'"
        ).all()
        products = connection.exec_driver_sql("SELECT count(*) FROM products").scalar_one()
    assert [tuple(row) for row in codes] == [("1111111", a, "backfill"), ("2222222", b, "backfill")]
    assert ([tuple(row) for row in stray], products) == ([(b,)], 2)
    assert [(split.to_product_id, split.code.code) for split in found.splits] == [(b, "2222222")]
    assert found.merge_candidates == ()


@pytest.mark.parametrize("more_first", [False, True], ids=["more-lines-higher-id", "lower-id"])
def test_i21_a_code_kept_by_two_products_goes_to_the_one_with_more_lines(
    open_db: Callable[[str], Db], plu: PluCatalog, more_first: bool
) -> None:
    """Tie: the lowest id (``tie`` row of the fakes suite); here the counts differ, 2 to 1."""
    db = open_db("two-keepers")
    first = db.add(legacy_product(canonical_name="First"))
    second = db.add(legacy_product(canonical_name="Second"))
    more, fewer = (first, second) if more_first else (second, first)
    db.buy(_item(0, C1, more), _item(1, C1, more), _item(2, C1, fewer))

    found = db.backfill(plu)

    with db.factory(read_only=True) as uow:
        row = uow.product_codes.find(CodeScheme.STORE, "costco", "1362394")
    assert row is not None
    assert row.product_id == more
    assert [(m.product_id, m.other_product_id) for m in found.merge_candidates] == [(more, fewer)]


@pytest.mark.parametrize("a_first", [True, False], ids=["a-lower-id", "b-lower-id"])
def test_i22_kept_codes_are_fixed_before_lines_move_in(
    open_db: Callable[[str], Db], plu: PluCatalog, a_first: bool
) -> None:
    """A has two X lines and a person's Y code row but no Y line; B has three Z lines and one Y
    line. Before any move A keeps X (an owned code counts only among the codes its lines carry)
    and B keeps Z. B's Y line moves to A, the product Y's row names; A's kept code stays X, so
    nothing splits off A, whichever product is visited first. A second apply changes nothing."""
    db = open_db("i22")
    names = ("Chocolate Pistachio Bars", "Zed Things")
    if a_first:
        a, b = (db.add(legacy_product(canonical_name=name)) for name in names)
    else:
        b, a = (db.add(legacy_product(canonical_name=name)) for name in reversed(names))
    db.store(
        lambda uow: uow.product_codes.upsert(
            ProductCode(
                None, a, CodeScheme.STORE, "costco", "1543210", CodeSource.MANUAL,
                CREATED_AT, CREATED_AT,
            ),
            replace=True,
        )
    )  # fmt: skip
    db.buy(_item(0, C1, a), _item(1, C1, a))
    db.buy(*(_item(n, "1111111 ZED THING", b) for n in range(3)), _item(3, C2, b), day=1)

    db.backfill(plu)
    db.clock.advance(timedelta(hours=1))
    second = db.backfill(plu)

    with db.engine.connect() as connection:
        codes = connection.exec_driver_sql(
            "SELECT code, product_id, source FROM product_codes ORDER BY code"
        ).all()
        owners = connection.exec_driver_sql(
            "SELECT raw_text, product_id FROM line_items ORDER BY receipt_id, position"
        ).all()
        products = connection.exec_driver_sql(
            "SELECT id, canonical_name FROM products ORDER BY id"
        ).all()
    assert [tuple(row) for row in codes] == [
        ("1111111", b, "backfill"),
        ("1362394", a, "backfill"),
        ("1543210", a, "manual"),
    ]
    assert [tuple(row) for row in owners] == [
        (C1, a), (C1, a),
        *[("1111111 ZED THING", b)] * 3, (C2, a),
    ]  # fmt: skip
    assert sorted(tuple(row) for row in products) == sorted([(a, "CHOC PISTACH"), (b, "ZED THING")])
    assert second.changes == 0
