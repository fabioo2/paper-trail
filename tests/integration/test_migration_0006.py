"""Revision 0006, product codes (barcode-lookup plan R3, A9, Q1; PC-TC-13..17, 19..21).

A real SQLite file in ``tmp_path``, seeded at 0005 with raw SQL. Expected lists are written out
here, not read from the migration. The populated survival test and the mid-swap failures are
PR 1b (PC-TC-101, 102).
"""

import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

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
def schema_head(tmp_path_factory: pytest.TempPathFactory) -> Schema:
    return _built_schema(tmp_path_factory, "head")


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

    command.upgrade(alembic_config(database), "head")

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

    command.upgrade(alembic_config(database), "head")

    with engine.begin() as connection:
        _product(connection, "Inserted after 0006")
        stamped = _field_sources(connection)
    assert stamped == [*(expected for _, expected in STAMPS), {}]


@pytest.fixture
def at_head(database: Path, engine: Engine) -> Engine:
    """Seeded at 0005 (lines: new, alias, NULL), upgraded to head."""
    _seed_0005(database, engine, ("new", "alias", None))
    command.upgrade(alembic_config(database), "head")
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
    command.upgrade(config, "head")
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
    command.upgrade(config, "head")
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
    database: Path, engine: Engine, schema_head: Schema
) -> None:
    seeded = _seed_0005(database, engine, ("new", "alias", None))
    config = alembic_config(database)
    command.upgrade(config, "head")
    command.downgrade(config, "0005")

    command.upgrade(config, "head")

    with engine.connect() as connection:
        rows = _all_rows(connection, (*SEEDED_TABLES, "product_codes"))
        assert (current_revision(engine), _schema(connection), _field_sources(connection)) == (
            "0006",
            schema_head,
            [expected for _, expected in STAMPS],
        )
    assert {**rows, "products": _without_field_sources(rows["products"])} == {
        **seeded,
        "product_codes": [],
    }
