"""Revision 0004, paid prices (discounts-and-paid-prices plan R6, A2; design section 6).

``line_items.applies_to_position`` (INTEGER NULL, set only on a discount line and never to its
own position) is added in place, so the FTS triggers and the incoming foreign keys survive.
``price_observations`` is rebuilt with ``discount_cents``, ``unit_discount_cents``,
``paid_cents`` and ``on_sale`` under CHECKs; existing rows get discount 0, paid = line total and
on_sale 0. Before any DDL the upgrade refuses a database holding an observation with a negative
line total, naming its ids. The downgrade drops the five columns and keeps every row.
"""

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import alembic_config, current_revision, head_revision
from tests.support import SCHEMA_HEAD

NOW = "2026-11-14T12:00:00.000000+00:00"
OBSERVATION_COLUMNS = ("discount_cents", "unit_discount_cents", "paid_cents", "on_sale")
NEW_OBSERVATION_VALUES = {"discount_cents": 0, "unit_discount_cents": 0, "on_sale": 0}

# (line total, discount, unit discount, paid, on_sale) -> accepted; each rejected row breaks
# exactly one rule.
OBSERVATION_CHECKS: list[tuple[str, tuple[int, int, int, int, int], bool]] = [
    ("discount above zero", (899, 1, 0, 900, 0), False),
    ("unit discount above zero", (899, 0, 1, 899, 0), False),
    ("paid is not line total + discount", (899, -100, -100, 899, 1), False),
    ("paid below zero", (399, -400, -400, -1, 1), False),
    ("on sale without a discount", (899, 0, 0, 899, 1), False),
    ("a discount not on sale", (899, -100, -100, 799, 0), False),
    ("discounted", (899, -100, -100, 799, 1), True),
    ("regular", (899, 0, 0, 899, 0), True),
]

# (kind, position, applies_to_position) -> accepted. Beyond Q12's two rules the DB refuses only a
# negative position (as the domain does); it cannot see the other rows.
LINE_LINK_CHECKS: list[tuple[str, tuple[str, int, int | None], bool]] = [
    ("a link on an item line", ("item", 1, 0), False),
    ("a link to its own position", ("discount", 1, 1), False),
    ("a link to a negative position", ("discount", 1, -1), False),
    ("an unlinked discount", ("discount", 1, None), True),
    ("a discount linked to an earlier line", ("discount", 1, 0), True),
    ("a discount linked to a position no line has", ("discount", 1, 5), True),
]


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "paper_trail.db"


@pytest.fixture
def engine(database: Path) -> Iterator[Engine]:
    engine = create_engine(database)
    yield engine
    engine.dispose()


def _rows(connection: Connection, table: str) -> list[dict[str, Any]]:
    result = connection.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY id')  # noqa: S608
    return [dict(row) for row in result.mappings()]


def _schema(connection: Connection) -> list[tuple[str, str, str]]:
    return [
        tuple(row)
        for row in connection.execute(
            text("SELECT type, name, coalesce(sql, '') FROM sqlite_master ORDER BY type, name")
        ).all()
    ]


def _indexes(connection: Connection, table: str) -> set[tuple[str, tuple[str, ...], bool]]:
    found = set()
    for row in connection.exec_driver_sql(f'PRAGMA index_list("{table}")').mappings():
        if row["origin"] == "pk":
            continue
        columns = tuple(
            info["name"]
            for info in connection.exec_driver_sql(f'PRAGMA index_info("{row["name"]}")').mappings()
        )
        found.add((row["name"], columns, bool(row["unique"])))
    return found


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
            "purchased_on": "2026-11-01",
            "created_at": NOW,
            "updated_at": NOW,
        },
    )


def _product(connection: Connection, name: str = "Eggs") -> int:
    return _insert(connection, "products", {"canonical_name": name, "created_at": NOW})


def _line(  # noqa: PLR0913 - one parameter per column a test varies
    connection: Connection,
    receipt_id: int,
    position: int,
    *,
    kind: str = "item",
    description: str = "Eggs",
    quantity: str = "1",
    unit_price: int | None = 899,
    line_total: int = 899,
    **extra: object,
) -> int:
    return _insert(
        connection,
        "line_items",
        {
            "receipt_id": receipt_id,
            "position": position,
            "kind": kind,
            "raw_text": description.upper(),
            "description": description,
            "quantity": quantity,
            "unit_price_cents": unit_price,
            "line_total_cents": line_total,
            **extra,
        },
    )


def _observation(  # noqa: PLR0913 - one parameter per column a test varies
    connection: Connection,
    product_id: int,
    receipt_id: int,
    line_id: int,
    *,
    quantity: str = "1",
    unit_price: int = 899,
    line_total: int = 899,
    **extra: object,
) -> int:
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
            "unit_price_cents": unit_price,
            "line_total_cents": line_total,
            "currency": "CAD",
            **extra,
        },
    )


def test_tc_71_72_existing_observations_are_regular_prices_after_the_upgrade(
    database: Path, engine: Engine
) -> None:
    config = alembic_config(database)
    command.upgrade(config, "0003")
    with engine.begin() as connection:
        receipt_id = _receipt(connection)
        product_id = _product(connection)
        for position, (quantity, unit_price, line_total) in enumerate(
            [("1", 899, 899), ("2", 329, 658), ("1.36", 150, 204)]
        ):
            line_id = _line(
                connection,
                receipt_id,
                position,
                quantity=quantity,
                unit_price=unit_price,
                line_total=line_total,
            )
            _observation(
                connection,
                product_id,
                receipt_id,
                line_id,
                quantity=quantity,
                unit_price=unit_price,
                line_total=line_total,
            )
        _line(connection, receipt_id, 3, kind="discount", unit_price=None, line_total=-500)
        lines_before = _rows(connection, "line_items")
        observations_before = _rows(connection, "price_observations")

    command.upgrade(config, "head")

    assert (SCHEMA_HEAD, head_revision(), current_revision(engine)) == ("0004", "0004", "0004")
    with engine.connect() as connection:
        assert _rows(connection, "line_items") == [
            {**row, "applies_to_position": None} for row in lines_before
        ]
        assert _rows(connection, "price_observations") == [
            {**row, **NEW_OBSERVATION_VALUES, "paid_cents": row["line_total_cents"]}
            for row in observations_before
        ]


def test_tc_120_a_negative_line_total_refuses_the_upgrade_before_any_ddl(
    database: Path, engine: Engine
) -> None:
    config = alembic_config(database)
    command.upgrade(config, "0003")
    with engine.begin() as connection:
        receipt_id = _receipt(connection)
        product_id = _product(connection)
        for position in range(8):
            line_total = -100 if position + 1 in (3, 7) else 899
            line_id = _line(connection, receipt_id, position, line_total=line_total)
            _observation(connection, product_id, receipt_id, line_id, line_total=line_total)
        schema_before = _schema(connection)
        rows_before = {
            table: _rows(connection, table)
            for table in ("receipts", "products", "line_items", "price_observations")
        }
        assert [row["id"] for row in rows_before["price_observations"]] == list(range(1, 9))

    with pytest.raises(Exception, match=r"\b3\b.*\b7\b"):
        command.upgrade(config, "head")

    assert current_revision(engine) == "0003"
    with engine.begin() as connection:
        assert _schema(connection) == schema_before
        assert {table: _rows(connection, table) for table in rows_before} == rows_before
        connection.execute(text("DELETE FROM price_observations WHERE id IN (3, 7)"))
    command.upgrade(config, "head")
    assert current_revision(engine) == SCHEMA_HEAD


def test_tc_73_downgrade_drops_the_new_columns_and_keeps_every_row(
    database: Path, engine: Engine
) -> None:
    config = alembic_config(database)
    command.upgrade(config, "head")
    with engine.begin() as connection:
        receipt_id = _receipt(connection)
        product_id = _product(connection)
        item_id = _line(connection, receipt_id, 0)
        _line(
            connection,
            receipt_id,
            1,
            kind="discount",
            unit_price=None,
            line_total=-500,
            applies_to_position=0,
        )
        _observation(
            connection,
            product_id,
            receipt_id,
            item_id,
            discount_cents=-500,
            unit_discount_cents=-500,
            paid_cents=399,
            on_sale=1,
        )
        lines_before = _rows(connection, "line_items")
        observations_before = _rows(connection, "price_observations")

    command.downgrade(config, "0003")

    assert current_revision(engine) == "0003"
    with engine.connect() as connection:
        assert _rows(connection, "line_items") == [
            {name: value for name, value in row.items() if name != "applies_to_position"}
            for row in lines_before
        ]
        assert _rows(connection, "price_observations") == [
            {name: value for name, value in row.items() if name not in OBSERVATION_COLUMNS}
            for row in observations_before
        ]


def _seed_one_line(engine: Engine, kind: str = "item") -> tuple[int, int, int]:
    with engine.begin() as connection:
        receipt_id = _receipt(connection)
        product_id = _product(connection)
        line_id = _line(connection, receipt_id, 0, kind=kind)
    return receipt_id, product_id, line_id


@pytest.mark.parametrize(
    ("values", "accepted"),
    [(values, accepted) for _, values, accepted in OBSERVATION_CHECKS],
    ids=[name for name, _, _ in OBSERVATION_CHECKS],
)
def test_tc_74_observation_checks(
    migrated_engine: Engine, values: tuple[int, int, int, int, int], accepted: bool
) -> None:
    receipt_id, product_id, line_id = _seed_one_line(migrated_engine)
    line_total, discount, unit_discount, paid, on_sale = values

    def insert() -> None:
        with migrated_engine.begin() as connection:
            _observation(
                connection,
                product_id,
                receipt_id,
                line_id,
                line_total=line_total,
                discount_cents=discount,
                unit_discount_cents=unit_discount,
                paid_cents=paid,
                on_sale=on_sale,
            )

    if not accepted:
        with pytest.raises(IntegrityError):
            insert()
        return
    insert()
    with migrated_engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT line_total_cents, discount_cents, unit_discount_cents, paid_cents, on_sale"
                " FROM price_observations"
            )
        ).all()
    assert [tuple(row) for row in stored] == [values]


@pytest.mark.parametrize(
    ("line", "accepted"),
    [(line, accepted) for _, line, accepted in LINE_LINK_CHECKS],
    ids=[name for name, _, _ in LINE_LINK_CHECKS],
)
def test_tc_75_line_link_checks(
    migrated_engine: Engine, line: tuple[str, int, int | None], accepted: bool
) -> None:
    receipt_id, _, _ = _seed_one_line(migrated_engine)
    kind, position, applies_to = line

    def insert() -> int:
        with migrated_engine.begin() as connection:
            return _line(
                connection,
                receipt_id,
                position,
                kind=kind,
                unit_price=None,
                line_total=-100,
                applies_to_position=applies_to,
            )

    if not accepted:
        with pytest.raises(IntegrityError):
            insert()
        return
    line_id = insert()
    with migrated_engine.connect() as connection:
        stored = connection.execute(
            text("SELECT kind, position, applies_to_position FROM line_items WHERE id = :id"),
            {"id": line_id},
        ).one()
    assert tuple(stored) == line


def test_tc_76_the_rebuilt_table_keeps_its_indexes_and_foreign_keys(
    database: Path, engine: Engine
) -> None:
    config = alembic_config(database)
    command.upgrade(config, "0003")
    with engine.connect() as connection:
        at_0003 = (
            _indexes(connection, "price_observations"),
            connection.exec_driver_sql('PRAGMA foreign_key_list("price_observations")').all(),
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        assert (
            _indexes(connection, "price_observations"),
            connection.exec_driver_sql('PRAGMA foreign_key_list("price_observations")').all(),
        ) == at_0003


def _fts_ids(connection: Connection, word: str) -> list[int]:
    rows = connection.execute(
        text("SELECT rowid FROM line_items_fts WHERE line_items_fts MATCH :word ORDER BY rowid"),
        {"word": word},
    )
    return [int(row[0]) for row in rows]


@pytest.mark.parametrize("revision", ["head", "0003"], ids=["after upgrade", "after downgrade"])
def test_tc_77_the_line_items_fts_triggers_still_fire(
    database: Path, engine: Engine, revision: str
) -> None:
    config = alembic_config(database)
    command.upgrade(config, "head")
    if revision != "head":
        command.downgrade(config, revision)
    with engine.begin() as connection:
        receipt_id = _receipt(connection)
        kiwi = _line(connection, receipt_id, 0, description="Kiwi", raw_text="1543210")
        pistachio = _line(connection, receipt_id, 1, description="Pistachio", raw_text="1362394")
        inserted = _fts_ids(connection, "kiwi")
        connection.execute(
            text("UPDATE line_items SET description = 'Mango' WHERE id = :id"), {"id": kiwi}
        )
        updated = (_fts_ids(connection, "kiwi"), _fts_ids(connection, "mango"))
        connection.execute(text("DELETE FROM line_items WHERE id = :id"), {"id": pistachio})
        deleted = _fts_ids(connection, "pistachio")

    assert (inserted, updated, deleted) == ([kiwi], ([], [kiwi]), [])
