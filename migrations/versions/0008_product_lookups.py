"""Products are looked up or guessed: remember what the product databases answered, and fold
the old ``off_llm`` field source into ``llm`` (claude-extraction plan D3, V5, V10).

- ``product_lookups``: one row per code and database (``off``, ``obf``, ``opf``,
  ``upcitemdb``), a hit with its payload or a ``not_found`` without one; an error is never a
  row.
- ``lookup_requests``: one row per request sent to a rate-limited database (UPCitemdb),
  whatever its answer: its daily quota is counted here.
- ``products.field_sources`` is JSON without a CHECK: every ``off_llm`` stamp becomes ``llm``
  (a model's wording of a lookup is a guess, never confirmed); every other stamp is kept.

The downgrade drops both tables and gives the old code sources it knows: ``llm`` becomes
``off_llm`` again and ``upc`` becomes ``off``.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15
"""

import json
from collections.abc import Mapping, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_SOURCES = {"off_llm": "llm"}
DOWNGRADE_SOURCES = {"llm": "off_llm", "upc": "off"}


def upgrade() -> None:
    op.execute(
        "CREATE TABLE product_lookups ("
        " id INTEGER NOT NULL,"
        " code TEXT NOT NULL CHECK (code <> '' AND code NOT GLOB '*[^0-9]*'),"
        " source TEXT NOT NULL CHECK (source IN ('off', 'obf', 'opf', 'upcitemdb')),"
        " state TEXT NOT NULL CHECK (state IN ('hit', 'not_found')),"
        " payload TEXT,"
        " fetched_at TEXT NOT NULL,"
        " PRIMARY KEY (id),"
        " UNIQUE (code, source),"
        " CHECK ((state = 'hit') = (payload IS NOT NULL)))"
    )
    op.create_index(
        "ix_product_lookups_source_fetched_at", "product_lookups", ["source", "fetched_at"]
    )
    op.execute(
        "CREATE TABLE lookup_requests ("
        " id INTEGER NOT NULL,"
        " source TEXT NOT NULL CHECK (source IN ('off', 'obf', 'opf', 'upcitemdb')),"
        " at TEXT NOT NULL,"
        " PRIMARY KEY (id))"
    )
    op.create_index("ix_lookup_requests_source_at", "lookup_requests", ["source", "at"])
    _rename_field_sources(UPGRADE_SOURCES)


def downgrade() -> None:
    _rename_field_sources(DOWNGRADE_SOURCES)
    op.drop_index("ix_lookup_requests_source_at", table_name="lookup_requests")
    op.execute("DROP TABLE lookup_requests")
    op.drop_index("ix_product_lookups_source_fetched_at", table_name="product_lookups")
    op.execute("DROP TABLE product_lookups")


def _rename_field_sources(renames: Mapping[str, str]) -> None:
    """Rewrite each product's ``field_sources`` JSON where a stamp is renamed, nothing else."""
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, field_sources FROM products")).all()
    for product_id, text in rows:
        sources = json.loads(text)
        renamed = {key: renames.get(value, value) for key, value in sources.items()}
        if renamed != sources:
            connection.execute(
                sa.text("UPDATE products SET field_sources = :sources WHERE id = :id"),
                {"sources": json.dumps(renamed, ensure_ascii=False), "id": product_id},
            )
