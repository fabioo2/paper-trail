"""Claude reads receipts: drop the product vectors, retire ``waiting_for_gpu``, record what each
Claude call cost (claude-extraction plan V1, V11, R2, D5).

- The ``products_vector_delete`` trigger goes first, then the ``product_vectors`` virtual table
  (its shadow tables go with it). The migration runner loads sqlite-vec (``load_sqlite_vec``),
  so an empty database still migrates through 0002, which creates the table.
- ``receipts.stage`` loses ``waiting_for_gpu``: a receipt still waiting for the GPU is marked
  ``failed`` (status and stage), and the CHECK is narrowed. The CHECK is a table constraint,
  which SQLite can neither alter nor drop a column from, and ``receipts`` is the parent of every
  foreign key, so the CHECK text is edited in ``sqlite_schema`` (SQLite's documented way for
  changes that leave the stored rows valid) and ``schema_version`` is bumped.
- ``claude_calls``: one row per Claude call, the month's spend is summed from it. A row is first
  a reservation (no tokens yet, the call's largest possible cost), then the call's real tokens
  and cost; a call that failed unbilled is deleted.

The downgrade drops ``claude_calls``, widens the CHECK back (the failed receipts stay failed)
and recreates the vector table and its trigger, the table empty: embeddings cannot be
recovered (back up the database before deploying).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSION = 768  # 0002's width, frozen here for the downgrade

# The stage CHECK as 0002 wrote it, around the retired value.
STAGES_0006 = "'queued', 'waiting_for_gpu', 'reading'"
STAGES = "'queued', 'reading'"


def upgrade() -> None:
    op.execute("DROP TRIGGER products_vector_delete")
    op.execute("DROP TABLE product_vectors")
    op.execute(
        "UPDATE receipts SET status = 'failed', stage = 'failed' WHERE stage = 'waiting_for_gpu'"
    )
    _replace_in_receipts_schema(STAGES_0006, STAGES)
    op.execute(
        "CREATE TABLE claude_calls ("
        " id INTEGER NOT NULL,"
        " at TEXT NOT NULL,"
        " model TEXT NOT NULL,"
        " input_tokens INTEGER CHECK (input_tokens >= 0),"
        " output_tokens INTEGER CHECK (output_tokens >= 0),"
        " cost_usd TEXT NOT NULL,"
        " PRIMARY KEY (id))"
    )
    op.create_index("ix_claude_calls_at", "claude_calls", ["at"])


def downgrade() -> None:
    op.drop_index("ix_claude_calls_at", table_name="claude_calls")
    op.execute("DROP TABLE claude_calls")
    _replace_in_receipts_schema(STAGES, STAGES_0006)
    op.execute(
        "CREATE VIRTUAL TABLE product_vectors"
        f" USING vec0(embedding float[{EMBEDDING_DIMENSION}] distance_metric=cosine)"
    )
    op.execute(
        "CREATE TRIGGER products_vector_delete AFTER DELETE ON products"
        " BEGIN DELETE FROM product_vectors WHERE rowid = old.id; END"
    )


def _replace_in_receipts_schema(old: str, new: str) -> None:
    """Swap ``old`` for ``new`` in the ``receipts`` CREATE TABLE text, exactly once."""
    connection = op.get_bind()
    schema = connection.execute(
        sa.text("SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'receipts'")
    ).scalar_one()
    if schema.count(old) != 1:
        msg = f"the receipts schema does not hold {old} exactly once"
        raise RuntimeError(msg)
    version = connection.execute(sa.text("PRAGMA schema_version")).scalar_one()
    connection.exec_driver_sql("PRAGMA writable_schema = ON")
    try:
        connection.execute(
            sa.text(
                "UPDATE sqlite_schema SET sql = :sql WHERE type = 'table' AND name = 'receipts'"
            ),
            {"sql": schema.replace(old, new)},
        )
        connection.exec_driver_sql(f"PRAGMA schema_version = {int(version) + 1}")
    finally:
        connection.exec_driver_sql("PRAGMA writable_schema = OFF")
