"""Revision 0006, product codes (barcode-lookup plan R3, A9, Q1; PC-TC-13..17, 19..21, 101,
102).

A real SQLite file in ``tmp_path``, seeded at 0005 with raw SQL. Expected lists are written out
here, not read from the migration. PC-TC-101: a populated database (observations with discount
fields, linked discount lines, every old ``match_source`` plus NULL, aliases, corrections) is
unchanged after upgrade, downgrade and a second upgrade, with its indexes, foreign keys and FTS
triggers. PC-TC-102: a failure after any step of the upgrade's swap, or of the downgrade, leaves
the starting revision, schema and rows (no temporary column), and the same command then succeeds.
"""

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import alembic.op
import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import alembic_config, current_revision

NOW = "2026-11-15T12:00:00.000000+00:00"
LEGACY = "legacy_model"
MATCH_SOURCES = {"memory", "catalog", "alias", "vector", "llm", "new"}
CODE_SOURCES = (
    "manual", "import", "adopted", "correction", "review", "receipt", "off", "plu", "backfill",
)  # fmt: skip
SEEDED_TABLES = ("receipts", "products", "line_items")

# (name, type, not null (a primary key counts), default, primary key position)
PRODUCT_CODES_COLUMNS = [
    ("id", "INTEGER", True, None, 1),
    ("product_id", "INTEGER", True, None, 0),
    ("scheme", "TEXT", True, None, 0),
    ("store_key", "TEXT", True, "''", 0),
    ("code", "TEXT", True, None, 0),
    ("source", "TEXT", True, None, 0),
    ("created_at", "TEXT", True, None, 0),
    ("updated_at", "TEXT", True, None, 0),
]

# A product at 0005 (brand, size_value, size_unit, category, tags) -> field_sources after 0006.
STAMPS: list[tuple[dict[str, Any], dict[str, str]]] = [
    (
        {"brand": "Acme", "size_value": "24", "size_unit": "each", "category": "dairy_eggs",
         "tags": '["organic"]'},
        {"canonical_name": LEGACY, "brand": LEGACY, "size": LEGACY, "category": LEGACY,
         "tags": LEGACY},
    ),
    ({}, {"canonical_name": LEGACY}),
    ({"size_value": "1.36"}, {"canonical_name": LEGACY, "size": LEGACY}),
    ({"size_unit": "kg"}, {"canonical_name": LEGACY}),
    ({"category": "produce", "tags": "[]"}, {"canonical_name": LEGACY, "category": LEGACY}),
]  # fmt: skip

type Schema = list[tuple[str, str, str]]
type Rows = dict[str, list[dict[str, Any]]]


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "paper_trail.db"


@pytest.fixture
def engine(database: Path) -> Iterator[Engine]:
    engine = create_engine(database)
    yield engine
    engine.dispose()


def _built_schema(factory: pytest.TempPathFactory, revision: str) -> Schema:
    database = factory.mktemp(f"fresh_{revision}") / "paper_trail.db"
    command.upgrade(alembic_config(database), revision)
    engine = create_engine(database)
    try:
        with engine.connect() as connection:
            return _schema(connection)
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def schema_0005(tmp_path_factory: pytest.TempPathFactory) -> Schema:
    return _built_schema(tmp_path_factory, "0005")


@pytest.fixture(scope="module")
def schema_0006(tmp_path_factory: pytest.TempPathFactory) -> Schema:
    return _built_schema(tmp_path_factory, "0006")


def _schema(connection: Connection) -> Schema:
    return [
        tuple(row)
        for row in connection.execute(
            text("SELECT type, name, coalesce(sql, '') FROM sqlite_master ORDER BY type, name")
        ).all()
    ]


def _rows(connection: Connection, table: str) -> list[dict[str, Any]]:
    result = connection.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY id')  # noqa: S608
    return [dict(row) for row in result.mappings()]


def _all_rows(connection: Connection, tables: tuple[str, ...] = SEEDED_TABLES) -> Rows:
    return {table: _rows(connection, table) for table in tables}


def _insert(connection: Connection, table: str, values: Mapping[str, object]) -> int:
    columns = ", ".join(values)
    marks = ", ".join(f":{name}" for name in values)
    statement = text(f"INSERT INTO {table} ({columns}) VALUES ({marks}) RETURNING id")  # noqa: S608
    return int(connection.execute(statement, dict(values)).scalar_one())


def _receipt(connection: Connection) -> int:
    return _insert(
        connection,
        "receipts",
        {
            "status": "completed",
            "stage": "done",
            "currency": "CAD",
            "store_key": "costco",
            "purchased_on": "2026-11-01",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )


def _product(connection: Connection, name: str = "Eggs", **fields: Any) -> int:
    return _insert(connection, "products", {"canonical_name": name, "created_at": NOW, **fields})


def _line(
    connection: Connection,
    receipt_id: int,
    position: int,
    product_id: int | None,
    match_source: str | None,
) -> int:
    return _insert(
        connection,
        "line_items",
        {
            "receipt_id": receipt_id,
            "position": position,
            "kind": "item",
            "raw_text": f"1362394 CHOC PISTACH {position}",
            "description": "Choc",
            "quantity": "1",
            "unit_price_cents": 899,
            "line_total_cents": 899,
            "product_id": product_id,
            "match_source": match_source,
        },
    )


def _code(  # noqa: PLR0913 - a test builder: overrides with defaults
    connection: Connection,
    product_id: int,
    scheme: str = "store",
    store_key: str | None = "costco",
    code: str | None = "1362394",
    *,
    source: str = "manual",
) -> int:
    values: dict[str, object] = {
        "product_id": product_id,
        "scheme": scheme,
        "code": code,
        "source": source,
        "created_at": NOW,
        "updated_at": NOW,
    }
    if store_key is not None:
        values["store_key"] = store_key
    return _insert(connection, "product_codes", values)


def _seed_0005(database: Path, engine: Engine, line_sources: tuple[str | None, ...]) -> Rows:
    """At 0005: one receipt, one product per STAMPS row (two lines on the first products) and
    one line per ``line_sources`` value; returns the seeded rows."""
    command.upgrade(alembic_config(database), "0005")
    with engine.begin() as connection:
        receipt_id = _receipt(connection)
        product_ids = [
            _product(connection, f"Product {index}", **fields)
            for index, (fields, _) in enumerate(STAMPS)
        ]
        for position, source in enumerate(line_sources):
            _line(connection, receipt_id, position, product_ids[position % 2], source)
        return _all_rows(connection)


def _field_sources(connection: Connection) -> list[dict[str, str]]:
    rows = connection.execute(text("SELECT field_sources FROM products ORDER BY id")).scalars()
    return [json.loads(value) for value in rows]


def _without_field_sources(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if key != "field_sources"} for row in rows]


def test_pc_tc_13_upgrade_creates_exactly_the_contract(database: Path, engine: Engine) -> None:
    seeded = _seed_0005(database, engine, ("new", "alias", None))

    command.upgrade(alembic_config(database), "0006")

    with engine.connect() as connection:
        columns = [
            (row["name"], row["type"].upper(), bool(row["notnull"] or row["pk"]),
             row["dflt_value"], row["pk"])
            for row in connection.exec_driver_sql('PRAGMA table_info("product_codes")').mappings()
        ]  # fmt: skip
        indexes = {
            row["name"]: bool(row["unique"])
            for row in connection.exec_driver_sql('PRAGMA index_list("product_codes")').mappings()
        }
        index_columns = [
            row["name"]
            for row in connection.exec_driver_sql(
                'PRAGMA index_info("ix_product_codes_product")'
            ).mappings()
        ]
        field_sources = {
            row["name"]: (bool(row["notnull"]), row["dflt_value"])
            for row in connection.exec_driver_sql('PRAGMA table_info("products")').mappings()
        }["field_sources"]
        names = set(
            connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
            .scalars()
            .all()
        )
        product_columns = {
            row["name"]
            for row in connection.exec_driver_sql('PRAGMA table_info("products")').mappings()
        }
        counts = {table: len(rows) for table, rows in _all_rows(connection).items()}

    assert current_revision(engine) == "0006"
    assert columns == PRODUCT_CODES_COLUMNS
    assert (indexes["ix_product_codes_product"], index_columns) == (False, ["product_id"])
    assert field_sources == (True, "'{}'")
    assert {"product_groups", "product_group_decisions"} & names == set()
    assert "group_id" not in product_columns
    assert counts == {table: len(rows) for table, rows in seeded.items()}


def test_pc_tc_14_existing_products_are_stamped_legacy_model_per_filled_field(
    database: Path, engine: Engine
) -> None:
    _seed_0005(database, engine, ())

    command.upgrade(alembic_config(database), "0006")

    with engine.begin() as connection:
        _product(connection, "Inserted after 0006")
        stamped = _field_sources(connection)
    assert stamped == [*(expected for _, expected in STAMPS), {}]


@pytest.fixture
def at_head(database: Path, engine: Engine) -> Engine:
    """Seeded at 0005 (lines: new, alias, NULL), upgraded to head."""
    _seed_0005(database, engine, ("new", "alias", None))
    command.upgrade(alembic_config(database), "0006")
    return engine


def _set_source(engine: Engine, value: str | None) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE line_items SET match_source = :v WHERE id = 1"), {"v": value}
        )


@pytest.mark.parametrize("value", ["catalog", None, "memory"])
def test_pc_tc_15_match_source_accepts_catalog(at_head: Engine, value: str | None) -> None:
    _set_source(at_head, value)

    with at_head.connect() as connection:
        stored = connection.execute(text("SELECT match_source FROM line_items WHERE id = 1"))
        assert stored.scalar_one() == value


def test_pc_tc_15_match_source_keeps_a_column_local_check(at_head: Engine) -> None:
    with pytest.raises(IntegrityError):
        _set_source(at_head, "bogus")
    with at_head.connect() as connection:
        sql = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'line_items'")
        ).scalar_one()
    lists = re.findall(r'"?match_source"?\s+IN\s*\(([^)]*)\)', sql, flags=re.IGNORECASE)
    local = re.search(
        r'"?match_source"?\s+[A-Z]+(\(\d+\))?[^,]*?CHECK\s*\(\s*"?match_source"?\s+IN',
        sql,
        flags=re.IGNORECASE,
    )
    assert len(lists) == 1
    assert set(re.findall(r"'([^']*)'", lists[0])) == MATCH_SOURCES
    assert local is not None


ACCEPTED_CODES: list[tuple[str, str | None, str]] = [
    ("gtin", "", "manual"),
    ("plu", "", "manual"),
    ("store", "costco", "manual"),
    *(("store", "costco", source) for source in CODE_SOURCES),
]


@pytest.mark.parametrize(("scheme", "store_key", "source"), ACCEPTED_CODES)
def test_pc_tc_16_product_codes_accepts(
    at_head: Engine, scheme: str, store_key: str | None, source: str
) -> None:
    with at_head.begin() as connection:
        _code(connection, 1, scheme, store_key, source=source)


def test_pc_tc_16_store_key_defaults_to_empty(at_head: Engine) -> None:
    with at_head.begin() as connection:
        code_id = _code(connection, 1, "gtin", None, "0060383064877")
        stored = connection.execute(
            text("SELECT store_key FROM product_codes WHERE id = :id"), {"id": code_id}
        ).scalar_one()
    assert stored == ""


@pytest.mark.parametrize(
    "values",
    [
        {"scheme": "store", "store_key": ""},
        {"scheme": "gtin", "store_key": "costco"},
        {"scheme": "plu", "store_key": "costco"},
        {"scheme": "upc", "store_key": ""},
        {"source": "legacy"},
        {"code": None},
        {"product_id": None},
        {"product_id": 999},
    ],
    ids=[
        "store without store_key",
        "gtin with a store",
        "plu with a store",
        "unknown scheme",
        "unknown source",
        "null code",
        "null product",
        "no such product",
    ],
)
def test_pc_tc_16_product_codes_refuses(at_head: Engine, values: dict[str, Any]) -> None:
    row: dict[str, Any] = {
        "product_id": 1,
        "scheme": "store",
        "store_key": "costco",
        "code": "1362394",
        "source": "manual",
        "created_at": NOW,
        "updated_at": NOW,
        **values,
    }
    with pytest.raises(IntegrityError), at_head.begin() as connection:
        _insert(connection, "product_codes", row)


@pytest.mark.parametrize("second_product", [1, 2], ids=["same product", "another product"])
def test_pc_tc_17_a_code_names_at_most_one_product(at_head: Engine, second_product: int) -> None:
    with at_head.begin() as connection:
        _code(connection, 1)
    with pytest.raises(IntegrityError), at_head.begin() as connection:
        _code(connection, second_product, source="receipt")


def test_pc_tc_17_same_digits_under_other_schemes_or_stores_coexist(at_head: Engine) -> None:
    with at_head.begin() as connection:
        _code(connection, 1, "store", "costco", "3101")
        _code(connection, 2, "store", "loblaws", "3101")
        _code(connection, 3, "plu", "", "3101")
        _code(connection, 4, "plu", "", "93101")
        _code(connection, 1, "gtin", "", "0060383064877")
        count = connection.execute(text("SELECT count(*) FROM product_codes")).scalar_one()
    assert count == 5


def test_pc_tc_19_downgrade_without_catalog_lines(
    database: Path, engine: Engine, schema_0005: Schema
) -> None:
    seeded = _seed_0005(database, engine, ("new", "alias", None))
    config = alembic_config(database)
    command.upgrade(config, "0006")
    with engine.begin() as connection:
        _code(connection, 1)
        _code(connection, 2, "gtin", "", "0060383064877")

    command.downgrade(config, "0005")

    with engine.connect() as connection:
        assert (current_revision(engine), _schema(connection), _all_rows(connection)) == (
            "0005",
            schema_0005,
            seeded,
        )
    with pytest.raises(IntegrityError):
        _set_source(engine, "catalog")


def test_pc_tc_20_downgrade_is_refused_while_lines_say_catalog(
    database: Path, engine: Engine, schema_0005: Schema
) -> None:
    seeded = _seed_0005(database, engine, ("new", "alias", None, "new", "new"))
    config = alembic_config(database)
    command.upgrade(config, "0006")
    with engine.begin() as connection:
        _code(connection, 1)
        connection.execute(
            text("UPDATE line_items SET match_source = 'catalog' WHERE id IN (4, 5)")
        )
        before = (_schema(connection), _all_rows(connection, (*SEEDED_TABLES, "product_codes")))

    with pytest.raises(Exception, match=r"(?s)(\b2\b.*catalog|catalog.*\b2\b)"):
        command.downgrade(config, "0005")

    with engine.begin() as connection:
        assert current_revision(engine) == "0006"
        assert (
            _schema(connection),
            _all_rows(connection, (*SEEDED_TABLES, "product_codes")),
        ) == before
        connection.execute(text("UPDATE line_items SET match_source = 'new' WHERE id IN (4, 5)"))
    command.downgrade(config, "0005")
    with engine.connect() as connection:
        assert (current_revision(engine), _schema(connection), _all_rows(connection)) == (
            "0005",
            schema_0005,
            seeded,
        )


def test_pc_tc_21_upgrade_downgrade_upgrade(
    database: Path, engine: Engine, schema_0006: Schema
) -> None:
    seeded = _seed_0005(database, engine, ("new", "alias", None))
    config = alembic_config(database)
    command.upgrade(config, "0006")
    command.downgrade(config, "0005")

    command.upgrade(config, "0006")

    with engine.connect() as connection:
        rows = _all_rows(connection, (*SEEDED_TABLES, "product_codes"))
        assert (current_revision(engine), _schema(connection), _field_sources(connection)) == (
            "0006",
            schema_0006,
            [expected for _, expected in STAMPS],
        )
    assert {**rows, "products": _without_field_sources(rows["products"])} == {
        **seeded,
        "product_codes": [],
    }


POPULATED_TABLES = (
    "receipts", "products", "line_items", "price_observations", "product_aliases", "corrections",
)  # fmt: skip
OLD_MATCH_SOURCES = ("memory", "alias", "vector", "llm", "new", None)
FOREIGN_KEY_INDEXES = {
    "ix_product_aliases_product_id",
    "ix_price_observations_receipt_id",
    "ix_jobs_receipt_id",
    "ix_review_items_receipt_id",
    "ix_corrections_receipt_id",
    "ix_corrections_line_item_id",
}
FTS_HITS = "SELECT rowid FROM line_items_fts WHERE line_items_fts MATCH :query ORDER BY rowid"

type LineTables = tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]


def _observation(  # noqa: PLR0913 - one observation's fields
    connection: Connection,
    receipt_id: int,
    line_id: int,
    product_id: int,
    *,
    discount: int,
    quantity: str = "1",
) -> int:
    total = 899
    return _insert(
        connection,
        "price_observations",
        {
            "product_id": product_id,
            "receipt_id": receipt_id,
            "line_item_id": line_id,
            "store_key": "costco",
            "observed_on": "2026-11-01",
            "quantity": quantity,
            "unit_price_cents": total,
            "line_total_cents": total,
            "currency": "CAD",
            "discount_cents": discount,
            "unit_discount_cents": discount,
            "paid_cents": total + discount,
            "on_sale": discount < 0,
        },
    )


def _seed_populated(database: Path, engine: Engine) -> Rows:
    """At 0005: the STAMPS products; item lines 0-5 with each old ``match_source`` and NULL;
    discount lines 6 and 7 linked to items 0 and 1; observations regular (line 2), discounted
    and on sale (lines 0 and 1, one weighed); a MATCHED and a USER_CORRECTION alias; a
    PRODUCT_LINK and a CATEGORY correction."""
    command.upgrade(alembic_config(database), "0005")
    with engine.begin() as connection:
        receipt_id = _receipt(connection)
        product_ids = [
            _product(connection, f"Product {index}", **fields)
            for index, (fields, _) in enumerate(STAMPS)
        ]
        lines = [
            _line(connection, receipt_id, position, product_ids[position % 2], source)
            for position, source in enumerate(OLD_MATCH_SOURCES)
        ]
        for position, target in ((6, 0), (7, 1)):
            _insert(
                connection,
                "line_items",
                {"receipt_id": receipt_id, "position": position, "kind": "discount",
                 "raw_text": f"TPD/1362394 {target}", "description": "Instant savings",
                 "quantity": "1", "line_total_cents": -400, "applies_to_position": target},
            )  # fmt: skip
        _observation(connection, receipt_id, lines[0], product_ids[0], discount=-400)
        _observation(
            connection, receipt_id, lines[1], product_ids[1], discount=-400, quantity="1.36"
        )
        _observation(connection, receipt_id, lines[2], product_ids[0], discount=0)
        for alias, source in (("choc pistach 0", "matched"), ("choc pistach 1", "user_correction")):
            _insert(
                connection,
                "product_aliases",
                {"product_id": product_ids[0], "alias": alias, "store_key": "costco",
                 "source": source, "created_at": NOW},
            )  # fmt: skip
        for kind, value, line_id in (("product_link", "1", lines[0]), ("category", "pantry", None)):
            _insert(
                connection,
                "corrections",
                {"kind": kind, "subject": "choc pistach 0", "old_value": None,
                 "new_value": value, "store_key": "costco", "receipt_id": receipt_id,
                 "line_item_id": line_id, "created_at": NOW},
            )  # fmt: skip
        return _all_rows(connection, POPULATED_TABLES)


def _line_tables(connection: Connection) -> LineTables:
    return (
        [tuple(row) for row in connection.exec_driver_sql('PRAGMA index_list("line_items")')],
        [tuple(row) for row in connection.exec_driver_sql('PRAGMA foreign_key_list("line_items")')],
    )


def _comparable(rows: Rows) -> Rows:
    return {**rows, "products": _without_field_sources(rows["products"])}


def _assert_survived(engine: Engine, seeded: Rows, line_tables: LineTables) -> None:
    """PC-TC-101's equality: the seeded rows, the foreign-key indexes, the line table's indexes
    and foreign keys, and the FTS index and its update trigger."""
    with engine.begin() as connection:
        rows = _all_rows(connection, POPULATED_TABLES)
        indexes = set(
            connection.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'"))
            .scalars()
            .all()
        )
        tables = _line_tables(connection)
        found = connection.execute(text(FTS_HITS), {"query": "pistach"}).scalars().all()
        connection.execute(text("UPDATE line_items SET raw_text = 'ZESTY WAFFLE' WHERE id = 2"))
        renamed = connection.execute(text(FTS_HITS), {"query": "zesty"}).scalars().all()
        old = connection.execute(text(FTS_HITS), {"query": "pistach"}).scalars().all()
    assert _comparable(rows) == _comparable(seeded)
    assert indexes >= FOREIGN_KEY_INDEXES
    assert tables == line_tables
    assert (found, renamed, old) == ([1, 2, 3, 4, 5, 6], [2], [1, 3, 4, 5, 6])


@pytest.mark.parametrize(
    "steps",
    [("0006",), ("0006", "0005"), ("0006", "0005", "0006")],
    ids=["after upgrade", "after downgrade", "after second upgrade"],
)
def test_pc_tc_101_a_populated_database_survives_every_step(
    database: Path, engine: Engine, steps: tuple[str, ...]
) -> None:
    seeded = _seed_populated(database, engine)
    with engine.connect() as connection:
        line_tables = _line_tables(connection)
    config = alembic_config(database)

    for target in steps:
        if target == "0006":
            command.upgrade(config, target)
        else:
            command.downgrade(config, target)

    assert current_revision(engine) == ("0005" if steps[-1] == "0005" else "0006")
    _assert_survived(engine, seeded, line_tables)


class InjectedFaultError(RuntimeError):
    """The patched migration step failed on purpose."""


@pytest.mark.parametrize(
    ("direction", "failing_sql"),
    [
        ("upgrade", "ALTER TABLE line_items ADD COLUMN match_source_0006 TEXT"),
        ("upgrade", "ALTER TABLE line_items DROP COLUMN match_source"),
        ("upgrade", "UPDATE line_items SET match_source = match_source_0006"),
        ("downgrade", "ALTER TABLE products DROP COLUMN field_sources"),
        ("downgrade", "ALTER TABLE line_items DROP COLUMN applies_to_position"),
    ],
    ids=[
        "upgrade after the temporary column",
        "upgrade after the original is dropped",
        "upgrade after the copy back",
        "downgrade after field_sources is dropped",
        "downgrade swap after the originals are dropped",
    ],
)
def test_pc_tc_102_a_failure_mid_migration_leaves_the_previous_revision_and_retries(
    database: Path,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
    failing_sql: str,
) -> None:
    seeded = _seed_populated(database, engine)
    with engine.connect() as connection:
        line_tables = _line_tables(connection)
    config = alembic_config(database)
    if direction == "downgrade":
        command.upgrade(config, "0006")
    start = current_revision(engine)
    with engine.connect() as connection:
        before = (_schema(connection), _all_rows(connection, POPULATED_TABLES))
    engine.dispose()
    real_execute = alembic.op.execute

    def failing(sql: Any, *args: Any, **kwargs: Any) -> None:
        real_execute(sql, *args, **kwargs)
        if str(sql) == failing_sql:
            raise InjectedFaultError(failing_sql)

    run = command.upgrade if direction == "upgrade" else command.downgrade
    target = "0006" if direction == "upgrade" else "0005"
    monkeypatch.setattr(alembic.op, "execute", failing)

    with pytest.raises(InjectedFaultError):
        run(config, target)

    monkeypatch.undo()
    reopened = create_engine(database)
    try:
        with reopened.connect() as connection:
            after = (_schema(connection), _all_rows(connection, POPULATED_TABLES))
            columns = [
                row["name"]
                for row in connection.exec_driver_sql('PRAGMA table_info("line_items")').mappings()
            ]
        assert current_revision(reopened) == start
        assert after == before
        assert [name for name in columns if name.endswith("_0006")] == []

        run(config, target)

        assert current_revision(reopened) == ("0006" if direction == "upgrade" else "0005")
        _assert_survived(reopened, seeded, line_tables)
    finally:
        reopened.dispose()
