"""Revision 0005, foreign-key indexes (reliability plan R7-R10, amendment A1).

The live database lacks 5 of the 6 foreign-key indexes 0002 creates (0002 was edited after it
was applied). 0005 creates all 6 with ``IF NOT EXISTS``: a database missing any of them ends up
with the same schema (``sqlite_master``, index SQL included) as a fresh one, and a fresh one is
unchanged. An index of one of those names with another definition stops the upgrade with an
error naming it, and nothing the upgrade created stays. The downgrade is a no-op: 0002 owns these
indexes on a fresh database.
"""

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import alembic_config, current_revision

NOW = "2026-11-14T12:00:00.000000+00:00"

# (name, table, columns, unique, partial), written out here rather than read from a migration.
FOREIGN_KEY_INDEXES = {
    ("ix_product_aliases_product_id", "product_aliases", ("product_id",), False, False),
    ("ix_price_observations_receipt_id", "price_observations", ("receipt_id",), False, False),
    ("ix_jobs_receipt_id", "jobs", ("receipt_id",), False, False),
    ("ix_review_items_receipt_id", "review_items", ("receipt_id",), False, False),
    ("ix_corrections_receipt_id", "corrections", ("receipt_id",), False, False),
    ("ix_corrections_line_item_id", "corrections", ("line_item_id",), False, False),
}
LIVE_MISSING = (
    "ix_product_aliases_product_id",
    "ix_jobs_receipt_id",
    "ix_review_items_receipt_id",
    "ix_corrections_receipt_id",
    "ix_corrections_line_item_id",
)
SEEDED_TABLES = (
    "receipts",
    "products",
    "line_items",
    "product_aliases",
    "price_observations",
    "jobs",
    "review_items",
    "corrections",
)

type IndexDefinition = tuple[str, str, tuple[str, ...], bool, bool]
type Schema = list[tuple[str, str, str]]


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "paper_trail.db"


@pytest.fixture
def engine(database: Path) -> Iterator[Engine]:
    engine = create_engine(database)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def fresh(tmp_path_factory: pytest.TempPathFactory) -> tuple[set[IndexDefinition], Schema]:
    """The index definitions and the schema of a database built from nothing to head."""
    database = tmp_path_factory.mktemp("fresh") / "paper_trail.db"
    command.upgrade(alembic_config(database), "head")
    engine = create_engine(database)
    try:
        with engine.connect() as connection:
            return _indexes(connection), _schema(connection)
    finally:
        engine.dispose()


@pytest.fixture
def fresh_schema(fresh: tuple[set[IndexDefinition], Schema]) -> Schema:
    return fresh[1]


def _indexes(connection: Connection) -> set[IndexDefinition]:
    """Every index in the database: name, table, columns in order, unique, partial."""
    found = set()
    for table in connection.execute(
        text("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    ).scalars():
        for row in connection.exec_driver_sql(f'PRAGMA index_list("{table}")').mappings():
            columns = tuple(
                info["name"]
                for info in connection.exec_driver_sql(
                    f'PRAGMA index_info("{row["name"]}")'
                ).mappings()
            )
            found.add((row["name"], table, columns, bool(row["unique"]), bool(row["partial"])))
    return found


def _schema(connection: Connection) -> Schema:
    return [
        tuple(row)
        for row in connection.execute(
            text("SELECT type, name, coalesce(sql, '') FROM sqlite_master ORDER BY type, name")
        ).all()
    ]


def _counts(connection: Connection) -> dict[str, int]:
    return {
        table: int(connection.exec_driver_sql(f'SELECT count(*) FROM "{table}"').scalar_one())  # noqa: S608
        for table in SEEDED_TABLES
    }


def _insert(connection: Connection, table: str, values: Mapping[str, object]) -> int:
    columns = ", ".join(values)
    marks = ", ".join(f":{name}" for name in values)
    statement = text(f"INSERT INTO {table} ({columns}) VALUES ({marks}) RETURNING id")  # noqa: S608
    return int(connection.execute(statement, dict(values)).scalar_one())


def _seed(connection: Connection) -> None:
    """One receipt with a row in every table a foreign-key index covers."""
    receipt_id = _insert(
        connection,
        "receipts",
        {
            "status": "completed",
            "stage": "done",
            "currency": "CAD",
            "purchased_on": "2026-11-01",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    product_id = _insert(connection, "products", {"canonical_name": "Eggs", "created_at": NOW})
    line_id = _insert(
        connection,
        "line_items",
        {
            "receipt_id": receipt_id,
            "position": 0,
            "kind": "item",
            "raw_text": "EGGS",
            "description": "Eggs",
            "quantity": "1",
            "unit_price_cents": 899,
            "line_total_cents": 899,
            "product_id": product_id,
        },
    )
    _insert(
        connection,
        "product_aliases",
        {"product_id": product_id, "alias": "eggs", "source": "import", "created_at": NOW},
    )
    _insert(
        connection,
        "price_observations",
        {
            "product_id": product_id,
            "receipt_id": receipt_id,
            "line_item_id": line_id,
            "store_key": "costco",
            "observed_on": "2026-11-01",
            "quantity": "1",
            "unit_price_cents": 899,
            "line_total_cents": 899,
            "currency": "CAD",
            "discount_cents": 0,
            "unit_discount_cents": 0,
            "paid_cents": 899,
            "on_sale": 0,
        },
    )
    _insert(
        connection,
        "jobs",
        {
            "kind": "process_receipt",
            "receipt_id": receipt_id,
            "status": "pending",
            "max_attempts": 3,
            "available_at": NOW,
            "created_at": NOW,
            "updated_at": NOW,
        },
    )
    _insert(
        connection,
        "review_items",
        {
            "receipt_id": receipt_id,
            "reason": "low_confidence",
            "details": "{}",
            "status": "open",
            "created_at": NOW,
        },
    )
    _insert(
        connection,
        "corrections",
        {
            "kind": "field",
            "subject": "total_cents",
            "new_value": "2683",
            "receipt_id": receipt_id,
            "line_item_id": line_id,
            "created_at": NOW,
        },
    )


def _at_0004_without(database: Path, engine: Engine, dropped: tuple[str, ...]) -> dict[str, int]:
    """A seeded database at 0004 missing the ``dropped`` indexes; returns its row counts."""
    command.upgrade(alembic_config(database), "0004")
    with engine.begin() as connection:
        _seed(connection)
        for name in dropped:
            connection.exec_driver_sql(f'DROP INDEX "{name}"')
        return _counts(connection)


def test_rl_tc_10_the_six_indexes_are_the_written_list(
    fresh: tuple[set[IndexDefinition], Schema],
) -> None:
    """Guards the parity tests below: a fresh database has exactly the 6 written here."""
    indexes, _ = fresh
    assert {
        index for index in indexes if index[0] in {row[0] for row in FOREIGN_KEY_INDEXES}
    } == FOREIGN_KEY_INDEXES


@pytest.mark.parametrize(
    "dropped",
    [
        LIVE_MISSING,
        (*LIVE_MISSING, "ix_price_observations_receipt_id"),
        LIVE_MISSING[:3],
    ],
    ids=["the live database's five", "all six", "three of the five"],
)
def test_rl_tc_10_the_upgrade_adds_the_missing_indexes(
    database: Path, engine: Engine, fresh_schema: Schema, dropped: tuple[str, ...]
) -> None:
    counts = _at_0004_without(database, engine, dropped)

    command.upgrade(alembic_config(database), "head")

    with engine.connect() as connection:
        assert (current_revision(engine), _schema(connection), _counts(connection)) == (
            "0005",
            fresh_schema,
            counts,
        )


def test_rl_tc_10_a_complete_database_is_unchanged(database: Path, engine: Engine) -> None:
    counts = _at_0004_without(database, engine, ())
    with engine.connect() as connection:
        before = _schema(connection)

    command.upgrade(alembic_config(database), "head")

    with engine.connect() as connection:
        assert (current_revision(engine), _schema(connection), _counts(connection)) == (
            "0005",
            before,
            counts,
        )


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        ("ix_jobs_receipt_id", "CREATE INDEX ix_jobs_receipt_id ON review_items (receipt_id)"),
        (
            "ix_corrections_line_item_id",
            "CREATE INDEX ix_corrections_line_item_id ON corrections (receipt_id)",
        ),
        (
            "ix_review_items_receipt_id",
            "CREATE INDEX ix_review_items_receipt_id ON review_items (receipt_id, status)",
        ),
        (
            "ix_corrections_line_item_id",
            "CREATE UNIQUE INDEX ix_corrections_line_item_id ON corrections (line_item_id)",
        ),
        (
            "ix_corrections_receipt_id",
            "CREATE INDEX ix_corrections_receipt_id ON corrections (receipt_id)"
            " WHERE receipt_id IS NOT NULL",
        ),
        ("ix_jobs_receipt_id", "CREATE INDEX ix_jobs_receipt_id ON jobs (receipt_id DESC)"),
        (
            "ix_corrections_receipt_id",
            "CREATE INDEX ix_corrections_receipt_id ON corrections (receipt_id COLLATE NOCASE)",
        ),
    ],
    ids=[
        "another table",
        "another column",
        "more columns",
        "unique",
        "partial",
        "descending",
        "another collation",
    ],
)
def test_rl_tc_10_an_index_of_that_name_with_another_definition_stops_the_upgrade(
    database: Path, engine: Engine, name: str, replacement: str
) -> None:
    """The first of the six is missing too, so the upgrade creates it before it meets the
    conflict: the refused upgrade must not keep it."""
    _at_0004_without(database, engine, (LIVE_MISSING[0], name))
    with engine.begin() as connection:
        connection.exec_driver_sql(replacement)
        before = _schema(connection)

    with pytest.raises(RuntimeError, match=rf"{name} exists but is not a plain index"):
        command.upgrade(alembic_config(database), "head")

    with engine.connect() as connection:
        assert (current_revision(engine), _schema(connection)) == ("0004", before)


@pytest.mark.parametrize("dropped", [(), LIVE_MISSING], ids=["fresh", "repaired"])
def test_rl_tc_11_downgrade_keeps_the_indexes_and_upgrade_runs_again(
    database: Path, engine: Engine, fresh_schema: Schema, dropped: tuple[str, ...]
) -> None:
    counts = _at_0004_without(database, engine, dropped)
    config = alembic_config(database)
    command.upgrade(config, "head")

    command.downgrade(config, "0004")

    with engine.connect() as connection:
        assert (current_revision(engine), _schema(connection), _counts(connection)) == (
            "0004",
            fresh_schema,
            counts,
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        assert (current_revision(engine), _schema(connection), _counts(connection)) == (
            "0005",
            fresh_schema,
            counts,
        )
