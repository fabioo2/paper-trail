"""Product ids are never reused (cleanup D: products merged into the same one).

A merged-away product is deleted; without AUTOINCREMENT the next product would take its id when
it was the highest, and anything still naming the old id (a graph checkpoint, a queued
enrichment target) would then name an unrelated product. ``products.id`` becomes
``PRIMARY KEY (id AUTOINCREMENT)`` and ``sqlite_sequence`` starts at the highest id stored.

The table is not rebuilt: migrations run in one transaction with foreign keys on, where dropping
``products`` would cascade to every line, price, code and alias. AUTOINCREMENT changes no stored
row or index, only how SQLite picks the next rowid, so the CREATE TABLE text is edited in
``sqlite_schema`` as 0007 does (``schema_version`` bumped); the foreign keys, indexes and FTS
triggers stay as they are. ``sqlite_sequence`` is created by creating (and dropping) a throwaway
AUTOINCREMENT table when none exists yet.

The downgrade puts the 0010 text back and forgets the sequence row.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEY_0010 = "PRIMARY KEY (id),"
KEY = "PRIMARY KEY (id AUTOINCREMENT),"


def upgrade() -> None:
    op.execute("CREATE TABLE _autoincrement_seed (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    op.execute("DROP TABLE _autoincrement_seed")
    _replace_in_products_schema(KEY_0010, KEY)
    op.execute(
        "INSERT INTO sqlite_sequence (name, seq)"
        " SELECT 'products', coalesce(max(id), 0) FROM products"
    )


def downgrade() -> None:
    op.execute("DELETE FROM sqlite_sequence WHERE name = 'products'")
    _replace_in_products_schema(KEY, KEY_0010)


def _replace_in_products_schema(old: str, new: str) -> None:
    """Swap ``old`` for ``new`` in the ``products`` CREATE TABLE text, exactly once."""
    connection = op.get_bind()
    schema = connection.execute(
        sa.text("SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'products'")
    ).scalar_one()
    if schema.count(old) != 1:
        msg = f"the products schema does not hold {old} exactly once"
        raise RuntimeError(msg)
    version = connection.execute(sa.text("PRAGMA schema_version")).scalar_one()
    connection.exec_driver_sql("PRAGMA writable_schema = ON")
    try:
        connection.execute(
            sa.text(
                "UPDATE sqlite_schema SET sql = :sql WHERE type = 'table' AND name = 'products'"
            ),
            {"sql": schema.replace(old, new)},
        )
        connection.exec_driver_sql(f"PRAGMA schema_version = {int(version) + 1}")
    finally:
        connection.exec_driver_sql("PRAGMA writable_schema = OFF")
