"""The migrated schema matches ``adapters/sqlite/schema.py`` (S1-AC-09, PLAN 3.11 risks).

``compare_metadata`` covers tables, columns, types, nullability and foreign keys of the regular
tables. Indexes are checked in ``test_migration_0002.py`` (SQLAlchemy cannot reflect the
expression and partial indexes). Each enum CHECK constraint's IN-list is read back from the
schema and must equal the enum's values exactly, so an added, renamed or removed enum member
without a migration fails here; the constraints are also exercised behaviourally.
"""

import re
import warnings
from enum import StrEnum
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from paper_trail.adapters.sqlite.schema import metadata
from paper_trail.domain.enums import (
    AliasSource,
    Category,
    CorrectionKind,
    JobKind,
    JobStatus,
    LineKind,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
    ReviewReason,
    ReviewStatus,
    Unit,
)

NOT_IN_METADATA = {
    "alembic_version",
    "line_items_fts",
    "products_fts",
    "product_vectors",
    # LangGraph's SqliteSaver tables (Stage 3) live in the same file but are not ours.
    "checkpoints",
    "writes",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
}

ENUM_COLUMNS: list[tuple[str, str, type[StrEnum]]] = [
    ("receipts", "status", ReceiptStatus),
    ("receipts", "stage", ProcessingStage),
    ("line_items", "kind", LineKind),
    ("line_items", "category", Category),
    ("line_items", "match_source", MatchSource),  # agent core migration 0003
    ("products", "size_unit", Unit),
    ("products", "category", Category),
    ("product_aliases", "source", AliasSource),
    ("jobs", "kind", JobKind),
    ("jobs", "status", JobStatus),
    ("review_items", "reason", ReviewReason),
    ("review_items", "status", ReviewStatus),
    ("corrections", "kind", CorrectionKind),
]

NOW = "2026-01-01T12:00:00.000000+00:00"

# One valid row per table, written with the column names of PLAN 3.5 (ids are all 1).
SEED_ROWS: list[tuple[str, dict[str, Any]]] = [
    ("receipts", {"status": "completed", "stage": "done", "store_name": "Costco",
                  "store_key": "costco", "purchased_on": "2026-11-01", "currency": "CAD",
                  "subtotal_cents": 899, "tax_cents": 0, "total_cents": 899,
                  "payment_last4": "1111", "ocr_text": "x", "image_path": None,
                  "created_at": NOW, "updated_at": NOW}),
    ("products", {"canonical_name": "Eggs", "brand": None, "size_value": "24",
                  "size_unit": "each", "category": "dairy_eggs", "tags": "[]",
                  "created_at": NOW}),
    ("line_items", {"receipt_id": 1, "position": 0, "kind": "item", "raw_text": "EGGS",
                    "description": "Eggs", "quantity": "1", "unit_price_cents": 899,
                    "line_total_cents": 899, "product_id": 1, "category": "dairy_eggs"}),
    ("product_aliases", {"product_id": 1, "alias": "eggs", "store_key": None,
                         "source": "import", "created_at": NOW}),
    ("price_observations", {"product_id": 1, "receipt_id": 1, "line_item_id": 1,
                            "store_key": "costco", "observed_on": "2026-11-01",
                            "quantity": "1", "unit_price_cents": 899,
                            "line_total_cents": 899, "currency": "CAD"}),
    ("jobs", {"kind": "process_receipt", "receipt_id": 1, "status": "pending", "attempts": 0,
              "max_attempts": 5, "available_at": NOW, "locked_by": None,
              "lease_expires_at": None, "last_error": None, "created_at": NOW,
              "updated_at": NOW}),
    ("review_items", {"receipt_id": 1, "reason": "validation_failed", "details": "{}",
                      "status": "resolved", "thread_id": None, "created_at": NOW,
                      "resolved_at": NOW}),
    ("corrections", {"kind": "category", "subject": "eggs", "old_value": None,
                     "new_value": "dairy_eggs", "store_key": None, "receipt_id": 1,
                     "line_item_id": 1, "created_at": NOW}),
]  # fmt: skip


SHADOW_TABLE_PREFIXES = ("line_items_fts_", "products_fts_", "product_vectors_")


def _include_name(name: str | None, type_: str, _parents: dict[str, str | None]) -> bool:
    if type_ == "table":
        table = name or ""
        return table not in NOT_IN_METADATA and not table.startswith(SHADOW_TABLE_PREFIXES)
    return type_ != "index"


def _include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    return type_ != "index"


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    """The migrated database with one committed row per table (all with id 1)."""
    with migrated_engine.begin() as connection:
        for table, row in SEED_ROWS:
            columns = ", ".join(row)
            placeholders = ", ".join(f":{column}" for column in row)
            connection.execute(
                text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"),  # noqa: S608
                row,
            )
    return migrated_engine


def _update(engine: Engine, table: str, column: str, value: object) -> None:
    """Set one column of row 1 and roll back: only the constraint check is of interest."""
    with engine.connect() as connection:
        try:
            connection.execute(
                text(f"UPDATE {table} SET {column} = :value WHERE id = 1"),  # noqa: S608
                {"value": value},
            )
        finally:
            connection.rollback()


def test_s1_ac_09_migrated_schema_matches_the_metadata(migrated_engine: Engine) -> None:
    """S1-AC-09: Alembic compare_metadata finds no differences in the regular tables."""
    with migrated_engine.connect() as connection, warnings.catch_warnings():
        # SQLAlchemy warns that it skips the expression-based alias index; indexes are
        # verified separately in test_migration_0002.py.
        warnings.filterwarnings("ignore", message=".*expression-based index.*")
        context = MigrationContext.configure(
            connection,
            opts={
                "include_name": _include_name,
                "include_object": _include_object,
                "compare_type": True,
            },
        )
        differences = compare_metadata(context, metadata)

    assert differences == []


def test_s1_ac_09_metadata_declares_exactly_the_regular_tables() -> None:
    """S1-AC-09: schema.py holds the eight regular tables and no virtual table."""
    assert set(metadata.tables) == {
        "receipts",
        "line_items",
        "products",
        "product_aliases",
        "price_observations",
        "jobs",
        "review_items",
        "corrections",
    }


def test_s1_ac_09_seed_rows_use_the_planned_columns(seeded: Engine) -> None:
    """S1-AC-09: one row per table with PLAN 3.5's column names inserts cleanly."""
    with seeded.connect() as connection:
        for table, _ in SEED_ROWS:
            count = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608
            assert count == 1, table


def _check_values(engine: Engine, table: str, column: str) -> set[str]:
    """The values of the ``<column> IN (...)`` CHECK constraint in ``table``'s schema."""
    with engine.connect() as connection:
        sql = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": table},
        ).scalar_one()
    pattern = rf'(?<!\w)"?{column}"?\s+IN\s*\(([^)]*)\)'
    lists = re.findall(pattern, sql, flags=re.IGNORECASE)
    assert len(lists) == 1, (table, column, lists)
    return set(re.findall(r"'([^']*)'", lists[0]))


@pytest.mark.parametrize(
    ("table", "column", "enum"), ENUM_COLUMNS, ids=[f"{t}.{c}" for t, c, _ in ENUM_COLUMNS]
)
def test_s1_ac_09_enum_check_lists_equal_the_enum_values(
    migrated_engine: Engine, table: str, column: str, enum: type[StrEnum]
) -> None:
    """PLAN 3.11: each CHECK list holds exactly the enum's values (no stale or missing one)."""
    assert _check_values(migrated_engine, table, column) == {member.value for member in enum}


@pytest.mark.parametrize(
    ("table", "column", "enum"), ENUM_COLUMNS, ids=[f"{t}.{c}" for t, c, _ in ENUM_COLUMNS]
)
def test_s1_ac_09_enum_check_constraints_match_the_enums(
    seeded: Engine, table: str, column: str, enum: type[StrEnum]
) -> None:
    """PLAN 3.11: each enum column accepts every enum value and rejects anything else."""
    for member in enum:
        _update(seeded, table, column, member.value)
    for bogus in ("bogus", next(iter(enum)).name, ""):
        with pytest.raises(IntegrityError):
            _update(seeded, table, column, bogus)


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("receipts", "currency", "CA"),
        ("receipts", "currency", "CADD"),
        ("receipts", "payment_last4", "123"),
        ("receipts", "payment_last4", "12a4"),
        ("receipts", "payment_last4", "12345"),
        ("receipts", "status", None),
        ("receipts", "created_at", None),
        ("line_items", "raw_text", None),
        ("line_items", "quantity", None),
        ("line_items", "line_total_cents", None),
        ("products", "canonical_name", None),
        ("products", "tags", None),
        ("product_aliases", "alias", None),
        ("price_observations", "store_key", None),
        ("price_observations", "unit_price_cents", None),
        ("jobs", "max_attempts", None),
        ("review_items", "details", None),
        ("corrections", "new_value", None),
    ],
)
def test_s1_ac_09_other_constraints(
    seeded: Engine, table: str, column: str, value: str | None
) -> None:
    """PLAN 3.5: currency length 3, payment_last4 four digits, NOT NULL columns."""
    with pytest.raises(IntegrityError):
        _update(seeded, table, column, value)


def test_s1_ac_09_foreign_keys_are_enforced(seeded: Engine) -> None:
    """PLAN 3.5: a line item must belong to an existing receipt."""
    with pytest.raises(IntegrityError):
        _update(seeded, "line_items", "receipt_id", 999)


def test_s1_ac_09_store_name_and_key_default_to_empty(seeded: Engine) -> None:
    """PLAN 3.5: store_name and store_key default to '' (a receipt being processed)."""
    with seeded.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO receipts (status, stage, currency, created_at, updated_at)"
                " VALUES ('processing', 'queued', 'CAD', :now, :now)"
            ),
            {"now": NOW},
        )
        row = connection.execute(
            text("SELECT store_name, store_key, total_cents FROM receipts WHERE id = 2")
        ).one()

    assert tuple(row) == ("", "", None)
