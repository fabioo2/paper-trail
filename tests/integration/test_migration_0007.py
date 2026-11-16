"""Revision 0007, no product vectors (claude-extraction plan V1, V11, R2, D5; CE-TC-045..051).

A real SQLite file in ``tmp_path``, seeded at 0006 with raw SQL on a connection that loads
sqlite-vec (``load_sqlite_vec``, as only the migration runner does). The seed is the old
pipeline's data: receipts with OCR text, lines matched by ``vector`` and ``llm``, aliases,
corrections, product codes, three product vectors and one receipt left waiting for the GPU.

- CE-TC-045: the upgrade drops the trigger and the vector table (and its shadow tables), adds
  ``claude_calls`` (ruling I1), narrows the stage CHECK (CQ13: a waiting receipt becomes
  failed) and leaves every other object and row as it was; a product can then be deleted.
- CE-TC-046: an empty file migrates to head through the runner (0002 needs sqlite-vec).
- CE-TC-047: the downgrade gives back 0006's schema with an empty vector table and every row,
  including one added at head; up, down, up equals one up.
- CE-TC-048: a failure after the trigger drop leaves 0006 intact, and the rerun succeeds.
- CE-TC-050: the application's engine at head has no ``vec_version`` and needs none.
- CE-TC-051: the old receipts load through the repository, their match sources and OCR text
  kept, and stay unchanged when a new receipt is stored.
"""

import json
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import alembic.op
import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import OperationalError

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import (
    alembic_config,
    current_revision,
    load_sqlite_vec,
    upgrade_to_head,
)
from paper_trail.domain.enums import MatchSource, ProcessingStage, ReceiptStatus
from tests.factories import product, receipt
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, sqlite_uow_factory
from tests.support import SCHEMA_HEAD, require_id

NOW = "2026-11-15T12:00:00.000000+00:00"
TABLES = (
    "receipts", "products", "line_items", "price_observations", "product_aliases",
    "corrections", "product_codes",
)  # fmt: skip
VECTOR_TRIGGER = ("trigger", "products_vector_delete")
SPEND_OBJECTS = {("table", "claude_calls"), ("index", "ix_claude_calls_at")}
OCR_TEXT = "COSTCO WHOLESALE #1234\nKS ORG EGGS 24CT 8.99"
WAITING_RECEIPT = 3

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


@pytest.fixture(scope="module")
def schema_0006(tmp_path_factory: pytest.TempPathFactory) -> Schema:
    database = tmp_path_factory.mktemp("fresh_0006") / "paper_trail.db"
    command.upgrade(alembic_config(database), "0006")
    engine = create_engine(database)
    try:
        with engine.connect() as connection:
            return _schema(connection)
    finally:
        engine.dispose()


def _schema(connection: Connection) -> Schema:
    return [
        tuple(row)
        for row in connection.execute(
            text(
                "SELECT type, name, coalesce(sql, '') FROM sqlite_master"
                " WHERE substr(name, 1, 7) != 'sqlite_' ORDER BY type, name"
            )
        ).all()
    ]


def _names(schema: Schema) -> set[tuple[str, str]]:
    return {(kind, name) for kind, name, _ in schema}


def _vector_objects(schema: Schema) -> set[tuple[str, str]]:
    """The vector table, its shadow tables and its delete trigger."""
    return {(kind, name) for kind, name in _names(schema) if name.startswith("product_vectors")} | {
        VECTOR_TRIGGER
    }


def _rows(connection: Connection, table: str) -> list[dict[str, Any]]:
    result = connection.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY id')  # noqa: S608
    return [dict(row) for row in result.mappings()]


def _all_rows(connection: Connection) -> Rows:
    return {table: _rows(connection, table) for table in TABLES}


def _insert(connection: Connection, table: str, values: Mapping[str, object]) -> int:
    columns = ", ".join(values)
    marks = ", ".join(f":{name}" for name in values)
    statement = text(f"INSERT INTO {table} ({columns}) VALUES ({marks}) RETURNING id")  # noqa: S608
    return int(connection.execute(statement, dict(values)).scalar_one())


def _receipt(connection: Connection, *, status: str = "completed", stage: str = "done") -> int:
    return _insert(
        connection,
        "receipts",
        {"status": status, "stage": stage, "store_name": "COSTCO WHOLESALE #1234",
         "store_key": "costco", "purchased_on": "2026-11-01", "currency": "CAD",
         "total_cents": 1798, "ocr_text": OCR_TEXT, "created_at": NOW, "updated_at": NOW},
    )  # fmt: skip


def _line(  # noqa: PLR0913 - one line's fields
    connection: Connection,
    receipt_id: int,
    position: int,
    product_id: int | None,
    match_source: str | None,
    *,
    raw_text: str = "KS ORG EGGS 24CT",
) -> int:
    return _insert(
        connection,
        "line_items",
        {"receipt_id": receipt_id, "position": position, "kind": "item", "raw_text": raw_text,
         "description": "Eggs", "quantity": "1", "unit_price_cents": 899,
         "line_total_cents": 899, "product_id": product_id, "match_source": match_source},
    )  # fmt: skip


def _vector(connection: Connection, product_id: int) -> None:
    embedding = [0.0] * 768
    embedding[product_id] = 1.0
    connection.execute(
        text("INSERT INTO product_vectors(rowid, embedding) VALUES (:id, :embedding)"),
        {"id": product_id, "embedding": json.dumps(embedding)},
    )


def _vector_count(connection: Connection) -> int:
    load_sqlite_vec(connection)
    count: int = connection.execute(text("SELECT count(*) FROM product_vectors")).scalar_one()
    return count


def _seed_0006(database: Path, engine: Engine) -> Rows:
    """At 0006: 3 products with vectors; receipt 1 (lines by vector, llm, new), receipt 2
    (alias, catalog), receipt 3 waiting for the GPU with no lines; an observation, two aliases,
    a correction and a product code. Returns the seeded rows."""
    command.upgrade(alembic_config(database), "0006")
    with engine.connect() as connection:
        load_sqlite_vec(connection)
        with connection.begin():
            products = [
                _insert(connection, "products", {"canonical_name": name, "created_at": NOW})
                for name in ("Eggs", "Milk", "Sourdough")
            ]
            for product_id in products:
                _vector(connection, product_id)
            first, second = _receipt(connection), _receipt(connection)
            _receipt(connection, status="processing", stage="waiting_for_gpu")
            lines = [
                _line(connection, first, 0, products[0], "vector"),
                _line(connection, first, 1, products[1], "llm", raw_text="KS 2% MILK 4L"),
                _line(connection, first, 2, products[2], "new", raw_text="KS SOURDOUGH"),
                _line(connection, second, 0, products[0], "alias"),
                _line(connection, second, 1, products[1], "catalog", raw_text="1362394 KS MILK"),
            ]
            _insert(
                connection,
                "price_observations",
                {"product_id": products[0], "receipt_id": first, "line_item_id": lines[0],
                 "store_key": "costco", "observed_on": "2026-11-01", "quantity": "1",
                 "unit_price_cents": 899, "line_total_cents": 899, "currency": "CAD",
                 "discount_cents": 0, "unit_discount_cents": 0, "paid_cents": 899,
                 "on_sale": False},
            )  # fmt: skip
            for alias, source in (("ks org eggs 24ct", "matched"), ("ks sourdough", "extracted")):
                _insert(
                    connection,
                    "product_aliases",
                    {"product_id": products[0], "alias": alias, "store_key": "costco",
                     "source": source, "created_at": NOW},
                )  # fmt: skip
            _insert(
                connection,
                "corrections",
                {"kind": "product_link", "subject": "ks org eggs 24ct", "old_value": None,
                 "new_value": str(products[0]), "store_key": "costco", "receipt_id": first,
                 "line_item_id": lines[0], "created_at": NOW},
            )  # fmt: skip
            _insert(
                connection,
                "product_codes",
                {"product_id": products[1], "scheme": "store", "store_key": "costco",
                 "code": "1362394", "source": "manual", "created_at": NOW, "updated_at": NOW},
            )  # fmt: skip
            assert _vector_count(connection) == 3
            return _all_rows(connection)


def _upgraded(seeded: Rows) -> Rows:
    """The seeded rows as 0007 leaves them: only the waiting receipt changes (CQ13)."""
    receipts = [
        {**row, "status": "failed", "stage": "failed"} if row["id"] == WAITING_RECEIPT else row
        for row in seeded["receipts"]
    ]
    return {**seeded, "receipts": receipts}


def _added_receipt(engine: Engine) -> Rows:
    with engine.begin() as connection:
        receipt_id = _receipt(connection)
        _line(connection, receipt_id, 0, 1, "catalog")
        return _all_rows(connection)


def test_ce_tc_045_upgrade_drops_the_vectors_and_keeps_everything_else(
    database: Path, engine: Engine, schema_0006: Schema
) -> None:
    seeded = _seed_0006(database, engine)

    command.upgrade(alembic_config(database), "head")

    with engine.connect() as connection:
        schema, rows = _schema(connection), _all_rows(connection)
    sql_0006 = {(kind, name): sql for kind, name, sql in schema_0006}
    sql_head = {(kind, name): sql for kind, name, sql in schema}
    vectors = _vector_objects(schema_0006)
    assert current_revision(engine) == SCHEMA_HEAD == "0007"
    assert {("table", "product_vectors"), VECTOR_TRIGGER} <= vectors
    assert _names(schema) == (_names(schema_0006) - vectors) | SPEND_OBJECTS
    assert [
        key
        for key, sql in sql_head.items()
        if key not in SPEND_OBJECTS | {("table", "receipts")} and sql != sql_0006[key]
    ] == []
    assert sql_head["table", "receipts"] == sql_0006["table", "receipts"].replace(
        "'waiting_for_gpu', ", ""
    )
    assert rows == _upgraded(seeded)

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM products WHERE id = 3"))
        remaining = connection.execute(text("SELECT id FROM products ORDER BY id")).scalars()
        assert list(remaining) == [1, 2]


def test_ce_tc_046_an_empty_database_migrates_to_head_through_the_runner(
    engine: Engine,
) -> None:
    """0002 creates the vector table with the runner's sqlite-vec, 0007 drops it. That the
    result equals the metadata is test_schema_matches_metadata (its template is built here)."""
    upgrade_to_head(engine)

    with engine.connect() as connection:
        names = _names(_schema(connection))
    assert current_revision(engine) == SCHEMA_HEAD
    assert [name for _, name in names if name.startswith("product_vectors")] == []
    assert VECTOR_TRIGGER not in names
    assert names >= SPEND_OBJECTS


def test_ce_tc_047_downgrade_gives_back_0006_with_an_empty_vector_table(
    database: Path, engine: Engine, schema_0006: Schema
) -> None:
    seeded = _seed_0006(database, engine)
    config = alembic_config(database)
    command.upgrade(config, "head")
    expected = _added_receipt(engine)

    command.downgrade(config, "0006")

    with engine.connect() as connection:
        schema, rows, vectors = (
            _schema(connection),
            _all_rows(connection),
            _vector_count(connection),
        )
    assert current_revision(engine) == "0006"
    assert schema == schema_0006
    assert vectors == 0
    assert rows == expected
    assert rows["receipts"][: len(seeded["receipts"])] == _upgraded(seeded)["receipts"]


def test_ce_tc_047_up_down_up_equals_one_up(
    database: Path, engine: Engine, tmp_path_factory: pytest.TempPathFactory
) -> None:
    seeded = _seed_0006(database, engine)
    config = alembic_config(database)
    once = tmp_path_factory.mktemp("once") / "paper_trail.db"
    command.upgrade(alembic_config(once), "head")
    reference = create_engine(once)
    try:
        with reference.connect() as connection:
            schema_head = _schema(connection)
    finally:
        reference.dispose()

    command.upgrade(config, "head")
    command.downgrade(config, "0006")
    command.upgrade(config, "head")

    with engine.connect() as connection:
        assert (_schema(connection), _all_rows(connection)) == (schema_head, _upgraded(seeded))


class InjectedFaultError(RuntimeError):
    """The patched migration step failed on purpose."""


def test_ce_tc_048_a_failure_after_the_trigger_drop_leaves_0006_and_retries(
    database: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = _seed_0006(database, engine)
    with engine.connect() as connection:
        before = _schema(connection)
    engine.dispose()
    config = alembic_config(database)
    real_execute = alembic.op.execute

    def failing(sql: Any, *args: Any, **kwargs: Any) -> None:
        real_execute(sql, *args, **kwargs)
        if str(sql) == "DROP TRIGGER products_vector_delete":
            raise InjectedFaultError(str(sql))

    monkeypatch.setattr(alembic.op, "execute", failing)

    with pytest.raises(InjectedFaultError):
        command.upgrade(config, "head")

    monkeypatch.undo()
    reopened = create_engine(database)
    try:
        with reopened.connect() as connection:
            after = (_schema(connection), _all_rows(connection), _vector_count(connection))
        assert current_revision(reopened) == "0006"
        assert after == (before, seeded, 3)

        command.upgrade(config, "head")

        assert current_revision(reopened) == SCHEMA_HEAD
        with reopened.connect() as connection:
            assert _all_rows(connection) == _upgraded(seeded)
    finally:
        reopened.dispose()


def test_ce_tc_050_the_app_engine_at_head_works_without_sqlite_vec(
    migrated_engine: Engine,
) -> None:
    uow_factory = sqlite_uow_factory(migrated_engine, FakeClock())
    with (
        migrated_engine.connect() as connection,
        pytest.raises(OperationalError, match="no such function"),
    ):
        connection.execute(text("SELECT vec_version()"))

    saved = in_unit(uow_factory, lambda uow: uow.receipts.add(receipt()))
    product_id = require_id(in_unit(uow_factory, lambda uow: uow.products.add(product())).id)
    renamed = in_unit(
        uow_factory,
        lambda uow: uow.products.update(
            replace(uow.products.get(product_id) or product(), canonical_name="Eggs 24")
        ),
    )
    with migrated_engine.begin() as connection:
        connection.execute(text("DELETE FROM products WHERE id = :id"), {"id": product_id})
        connection.execute(text("DELETE FROM receipts WHERE id = :id"), {"id": saved.id})

    with uow_factory(read_only=True) as uow:
        assert renamed.canonical_name == "Eggs 24"
        assert (uow.products.get(product_id), uow.receipts.get(require_id(saved.id))) == (
            None,
            None,
        )


def test_ce_tc_051_old_receipts_and_match_sources_load_and_stay_unchanged(
    database: Path, engine: Engine
) -> None:
    seeded = _seed_0006(database, engine)
    command.upgrade(alembic_config(database), "head")
    uow_factory = sqlite_uow_factory(engine, FakeClock())

    with uow_factory(read_only=True) as uow:
        first, waiting = uow.receipts.get(1), uow.receipts.get(WAITING_RECEIPT)
    in_unit(uow_factory, lambda uow: uow.receipts.add(receipt()))

    assert first is not None
    assert waiting is not None
    assert first.ocr_text == OCR_TEXT
    assert [item.match_source for item in first.items] == [
        MatchSource.VECTOR,
        MatchSource.LLM,
        MatchSource.NEW,
    ]
    assert (waiting.status, waiting.stage) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    with engine.connect() as connection:
        rows = _all_rows(connection)
    expected = _upgraded(seeded)
    assert rows["receipts"][: len(expected["receipts"])] == expected["receipts"]
    assert {table: rows[table] for table in TABLES if table not in {"receipts", "line_items"}} == {
        table: expected[table] for table in TABLES if table not in {"receipts", "line_items"}
    }
    assert rows["line_items"][: len(expected["line_items"])] == expected["line_items"]
