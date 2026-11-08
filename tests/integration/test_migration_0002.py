"""Revision 0002, the core schema: every object in PLAN 3.5, and a clean downgrade (S1-AC-09).

Index names are left to the implementer; indexes are identified by their table, key columns
(with DESC), uniqueness and partial WHERE clause.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import alembic_config, current_revision, head_revision
from tests.support import SCHEMA_HEAD

REGULAR_TABLES = {
    "receipts",
    "line_items",
    "products",
    "product_aliases",
    "price_observations",
    "jobs",
    "review_items",
    "corrections",
}
VIRTUAL_TABLES = {"line_items_fts", "products_fts", "product_vectors"}
SHADOW_PREFIXES = tuple(f"{name}_" for name in VIRTUAL_TABLES)
# SQLite's own bookkeeping, e.g. sqlite_sequence (created by vec0, and never droppable).
SQLITE_INTERNAL = "sqlite_"

# table -> {(key columns, unique, partial)}; "<expr>" stands for an expression column.
EXPECTED_INDEXES: dict[str, set[tuple[tuple[str, ...], bool, bool]]] = {
    "receipts": {
        (("created_at DESC", "id DESC"), False, False),
        (("status",), False, False),
        (("store_key",), False, False),
        (("purchased_on",), False, False),
    },
    "line_items": {(("receipt_id", "position"), True, False), (("product_id",), False, False)},
    "product_aliases": {(("alias", "<expr>"), True, False), (("product_id",), False, False)},
    "price_observations": {
        (("line_item_id",), True, False),
        (("product_id", "observed_on"), False, False),
        (("receipt_id",), False, False),
    },
    "jobs": {(("status", "available_at", "id"), False, False), (("receipt_id",), False, False)},
    "review_items": {(("receipt_id",), True, True), (("receipt_id",), False, False)},
    "corrections": {
        (("kind", "subject", "store_key", "created_at"), False, False),
        (("receipt_id",), False, False),
        (("line_item_id",), False, False),
    },
}

TRIGGERS_PER_TABLE = {"line_items": 3, "products": 4}  # FTS insert/update/delete (+ vec delete)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "paper_trail.db"


@pytest.fixture
def engine(database: Path) -> Iterator[Engine]:
    engine = create_engine(database)
    yield engine
    engine.dispose()


def _objects(connection: Connection, kind: str) -> dict[str, str]:
    rows = connection.execute(
        text("SELECT name, coalesce(sql, '') FROM sqlite_master WHERE type = :kind"), {"kind": kind}
    )
    return dict(rows.all())


def _application_tables(connection: Connection) -> set[str]:
    return {
        name
        for name in _objects(connection, "table")
        if name != "alembic_version"
        and not name.startswith(SHADOW_PREFIXES)
        and not name.startswith(SQLITE_INTERNAL)
    }


def _indexes(connection: Connection, table: str) -> set[tuple[tuple[str, ...], bool, bool]]:
    found = set()
    for row in connection.exec_driver_sql(f'PRAGMA index_list("{table}")').mappings():
        columns = tuple(
            ("<expr>" if info["cid"] == -2 else info["name"]) + (" DESC" if info["desc"] else "")
            for info in connection.exec_driver_sql(
                f'PRAGMA index_xinfo("{row["name"]}")'
            ).mappings()
            if info["key"]
        )
        found.add((columns, bool(row["unique"]), bool(row["partial"])))
    return found


def test_s1_ac_09_head_is_the_newest_revision() -> None:
    """S1-AC-09: the Alembic head is the newest revision the suites expect (0003 since the
    agent core; 0002_core_schema follows the baseline, see test_migrations_frozen.py)."""
    assert head_revision() == SCHEMA_HEAD


def test_s1_ac_09_upgrade_creates_every_table(database: Path, engine: Engine) -> None:
    """S1-AC-09: the eight regular tables and three virtual tables of PLAN 3.5 exist."""
    command.upgrade(alembic_config(database), "head")

    with engine.connect() as connection:
        tables = _objects(connection, "table")
        assert _application_tables(connection) == REGULAR_TABLES | VIRTUAL_TABLES
    for name in VIRTUAL_TABLES:
        assert tables[name].upper().startswith("CREATE VIRTUAL TABLE"), name


def test_s1_ac_09_fts_tables_are_external_content_with_the_planned_tokenizer(
    migrated_engine: Engine,
) -> None:
    """S1-AC-09: fts5 over line_items/products, unicode61 remove_diacritics 2, prefix 2 3."""
    with migrated_engine.connect() as connection:
        tables = _objects(connection, "table")

    for name, content, columns in [
        ("line_items_fts", "line_items", ("description", "raw_text")),
        ("products_fts", "products", ("canonical_name", "brand", "tags")),
    ]:
        sql = " ".join(tables[name].lower().replace('"', "'").split())
        assert "using fts5" in sql, sql
        assert f"content='{content}'" in sql or f"content={content}" in sql, sql
        assert "content_rowid='id'" in sql or "content_rowid=id" in sql, sql
        assert "remove_diacritics 2" in sql, sql
        assert "prefix='2 3'" in sql, sql
        for column in columns:
            assert column in sql, (name, column)


def test_s1_ac_09_vector_table_is_768_cosine(migrated_engine: Engine) -> None:
    """S1-AC-09: product_vectors is vec0(embedding float[768] distance_metric=cosine)."""
    with migrated_engine.connect() as connection:
        sql = " ".join(_objects(connection, "table")["product_vectors"].lower().split())

    assert "using vec0" in sql
    assert "embedding float[768]" in sql
    assert "distance_metric=cosine" in sql


def _foreign_key_columns(connection: Connection, table: str) -> set[str]:
    rows = connection.exec_driver_sql(f'PRAGMA foreign_key_list("{table}")').mappings()
    return {row["from"] for row in rows}


@pytest.mark.parametrize("table", sorted(REGULAR_TABLES))
def test_s1_ac_09_every_foreign_key_column_leads_an_index(
    migrated_engine: Engine, table: str
) -> None:
    """Cascades and lookups by parent never scan a child table: each FK column leads an index.

    A partial index does not count: it only covers some rows.
    """
    with migrated_engine.connect() as connection:
        leading = {
            columns[0]
            for columns, _, partial in _indexes(connection, table)
            if columns and not partial
        }
        foreign_keys = _foreign_key_columns(connection, table)

    assert foreign_keys <= leading, foreign_keys - leading


@pytest.mark.parametrize("table", sorted(EXPECTED_INDEXES))
def test_s1_ac_09_indexes(migrated_engine: Engine, table: str) -> None:
    """S1-AC-09: every index and unique constraint of PLAN 3.5 exists."""
    with migrated_engine.connect() as connection:
        found = _indexes(connection, table)

    assert EXPECTED_INDEXES[table] <= found, found


def test_s1_ac_09_alias_uniqueness_treats_null_store_as_a_key(migrated_engine: Engine) -> None:
    """S1-AC-09: UNIQUE(alias, COALESCE(store_key, '')) and the open-review partial index."""
    with migrated_engine.connect() as connection:
        sql = " ".join(
            " ".join(_objects(connection, "index").values()).lower().replace('"', "").split()
        )

    assert "coalesce(store_key, '')" in sql or "coalesce(store_key,'')" in sql
    assert "where status = 'open'" in sql or "where status='open'" in sql


def test_s1_ac_09_triggers_keep_fts_and_vectors_in_sync(migrated_engine: Engine) -> None:
    """S1-AC-09: FTS triggers on line_items and products, and the vector delete trigger."""
    with migrated_engine.connect() as connection:
        rows = connection.execute(
            text("SELECT tbl_name, sql FROM sqlite_master WHERE type = 'trigger'")
        ).all()

    per_table: dict[str, list[str]] = {}
    for table, sql in rows:
        per_table.setdefault(table, []).append(" ".join(sql.lower().split()))
    assert {table: len(sqls) for table, sqls in per_table.items()} == TRIGGERS_PER_TABLE
    for table in ("line_items", "products"):
        events = " ".join(per_table[table])
        for event in ("after insert", "after update", "after delete"):
            assert event in events, (table, event)
    assert any("product_vectors" in sql and "after delete" in sql for sql in per_table["products"])


def test_s1_ac_09_downgrade_to_0001_removes_everything(database: Path, engine: Engine) -> None:
    """S1-AC-09: downgrade to 0001 drops every table, index, trigger and virtual table."""
    config = alembic_config(database)
    command.upgrade(config, "head")

    command.downgrade(config, "0001")

    assert current_revision(engine) == "0001"
    with engine.connect() as connection:
        leftovers = connection.execute(
            text(
                "SELECT type, name FROM sqlite_master"
                " WHERE name != 'alembic_version' AND substr(name, 1, 7) != 'sqlite_'"
            )
        ).all()
    assert leftovers == []


def test_s1_ac_09_upgrade_downgrade_upgrade(database: Path, engine: Engine) -> None:
    """S1-AC-09: upgrade again after a downgrade succeeds and yields the same schema."""
    config = alembic_config(database)
    command.upgrade(config, "head")
    with engine.connect() as connection:
        first = connection.execute(
            text("SELECT type, name, sql FROM sqlite_master ORDER BY name")
        ).all()

    command.downgrade(config, "base")
    command.upgrade(config, "head")

    with engine.connect() as connection:
        second = connection.execute(
            text("SELECT type, name, sql FROM sqlite_master ORDER BY name")
        ).all()
    assert second == first
    assert current_revision(engine) == SCHEMA_HEAD
