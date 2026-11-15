"""Product codes: the code printed on a line identifies its product (barcode-lookup plan 3.2,
ADR-0018).

``product_codes`` maps a code to the one product it names: a GTIN or PLU at every store
(``store_key = ''``), a store's own code at that store. ``line_items.match_source`` gains
``catalog``: SQLite cannot alter a CHECK, so the column is copied to a temporary one, dropped,
re-added with the wider CHECK, copied back and the temporary column dropped, all in one
transaction and without rebuilding the table (its FTS triggers and foreign keys stay).
``products.field_sources`` records where each field came from; every existing product is
stamped ``legacy_model`` for each filled field (``size`` when ``size_value`` is set), so nothing
a model wrote before counts as confirmed.

The downgrade refuses, before any change, while a line has ``match_source = 'catalog'``,
naming how many; otherwise it drops what the upgrade added and restores the old CHECK.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-15
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MATCH_SOURCES_0003 = ("memory", "alias", "vector", "llm", "new")
MATCH_SOURCES = ("memory", "catalog", "alias", "vector", "llm", "new")
CODE_SCHEMES = ("gtin", "plu", "store")
CODE_SOURCES = (
    "manual",
    "import",
    "adopted",
    "correction",
    "review",
    "receipt",
    "off",
    "plu",
    "backfill",
)


# 0004's definition of applies_to_position, re-added after match_source on the downgrade so the
# table's columns (and its sqlite_master text) are back in their 0005 order.
APPLIES_TO_POSITION = (
    "applies_to_position INTEGER CHECK ("
    "applies_to_position IS NULL OR (kind = 'discount' AND applies_to_position >= 0"
    " AND applies_to_position <> position))"
)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _match_source(values: Sequence[str]) -> str:
    return f"match_source TEXT CHECK (match_source IN ({_quoted(values)}))"


def upgrade() -> None:
    op.execute(
        "CREATE TABLE product_codes ("
        " id INTEGER NOT NULL,"
        " product_id INTEGER NOT NULL,"
        f" scheme TEXT NOT NULL CHECK (scheme IN ({_quoted(CODE_SCHEMES)})),"
        " store_key TEXT NOT NULL DEFAULT '',"
        " code TEXT NOT NULL,"
        f" source TEXT NOT NULL CHECK (source IN ({_quoted(CODE_SOURCES)})),"
        " created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL,"
        " PRIMARY KEY (id),"
        " FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE CASCADE,"
        " CHECK ((scheme = 'store') = (store_key <> '')),"
        " UNIQUE (scheme, store_key, code))"
    )
    op.create_index("ix_product_codes_product", "product_codes", ["product_id"])
    _swap_columns(["match_source"], [_match_source(MATCH_SOURCES)])
    op.execute("ALTER TABLE products ADD COLUMN field_sources TEXT NOT NULL DEFAULT '{}'")
    _stamp_legacy_products()


def downgrade() -> None:
    connection = op.get_bind()
    catalog = connection.execute(
        sa.text("SELECT count(*) FROM line_items WHERE match_source = 'catalog'")
    ).scalar_one()
    if catalog:
        msg = (
            f"{catalog} line items have match_source 'catalog', which 0005 cannot store;"
            " set them to 'new', then downgrade again"
        )
        raise RuntimeError(msg)
    op.execute("ALTER TABLE products DROP COLUMN field_sources")
    _swap_columns(
        ["match_source", "applies_to_position"],
        [_match_source(MATCH_SOURCES_0003), APPLIES_TO_POSITION],
    )
    op.drop_index("ix_product_codes_product", table_name="product_codes")
    op.execute("DROP TABLE product_codes")


def _swap_columns(columns: Sequence[str], definitions: Sequence[str]) -> None:
    """Re-add ``columns`` of ``line_items`` with ``definitions``, in that order at the end of the
    table, keeping every value: copied to temporary columns, dropped, re-added, copied back."""
    for column in columns:
        op.execute(f"ALTER TABLE line_items ADD COLUMN {column}_0006 {_type(column)}")
        op.execute(f"UPDATE line_items SET {column}_0006 = {column}")  # noqa: S608
        op.execute(f"ALTER TABLE line_items DROP COLUMN {column}")
    for column, definition in zip(columns, definitions, strict=True):
        op.execute(f"ALTER TABLE line_items ADD COLUMN {definition}")
        op.execute(f"UPDATE line_items SET {column} = {column}_0006")  # noqa: S608
        op.execute(f"ALTER TABLE line_items DROP COLUMN {column}_0006")


def _type(column: str) -> str:
    return "INTEGER" if column == "applies_to_position" else "TEXT"


def _stamp_legacy_products() -> None:
    """``legacy_model`` for each filled field of every existing product."""
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, brand, size_value, category, tags FROM products ORDER BY id")
    ).mappings()
    for row in rows.all():
        filled = {
            "canonical_name": True,
            "brand": row["brand"] is not None,
            "size": row["size_value"] is not None,
            "category": row["category"] is not None,
            "tags": bool(json.loads(row["tags"])),
        }
        sources = {key: "legacy_model" for key, is_filled in filled.items() if is_filled}
        connection.execute(
            sa.text("UPDATE products SET field_sources = :sources WHERE id = :id"),
            {"sources": json.dumps(sources), "id": row["id"]},
        )
