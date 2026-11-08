"""Core schema: receipts, line items, products, aliases, prices, jobs, reviews, corrections,
plus the FTS5 indexes of line items and products and the sqlite-vec product vectors.

Enum values are frozen here as literals: a migration never imports application code, so a
later change to an enum needs a new migration (PLAN 3.5).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATEGORIES = (
    "produce", "meat_seafood", "dairy_eggs", "bakery", "deli_prepared", "pantry", "frozen",
    "beverages", "snacks_candy", "alcohol", "household", "personal_care", "health_pharmacy",
    "baby", "pet", "electronics", "clothing", "home_garden", "automotive_fuel", "restaurant",
    "services", "other",
)  # fmt: skip
LINE_KINDS = ("item", "discount", "deposit", "fee")
UNITS = ("each", "g", "kg", "ml", "l", "lb", "oz")
RECEIPT_STATUSES = ("processing", "needs_review", "completed", "failed")
PROCESSING_STAGES = (
    "queued", "waiting_for_gpu", "reading", "extracting", "validating", "matching", "enriching",
    "saving", "done", "needs_review", "failed",
)  # fmt: skip
JOB_KINDS = ("process_receipt",)
JOB_STATUSES = ("pending", "running", "succeeded", "failed")
REVIEW_REASONS = ("validation_failed", "low_confidence", "extraction_failed", "unmatched_product")
REVIEW_STATUSES = ("open", "resolved", "dismissed")
ALIAS_SOURCES = ("import", "extracted", "matched", "user_correction")
CORRECTION_KINDS = ("product_link", "category", "field")

# Foreign key columns not already leading a unique or lookup index.
FOREIGN_KEYS_TO_INDEX = (
    ("product_aliases", "product_id"),
    ("price_observations", "receipt_id"),
    ("jobs", "receipt_id"),
    ("review_items", "receipt_id"),
    ("corrections", "receipt_id"),
    ("corrections", "line_item_id"),
)

EMBEDDING_DIMENSION = 768  # nomic-embed-text
FTS_OPTIONS = "tokenize='unicode61 remove_diacritics 2', prefix='2 3'"

# External-content FTS5 tables and the columns of their content tables they index.
FTS_TABLES = {
    "line_items_fts": ("line_items", ("description", "raw_text")),
    "products_fts": ("products", ("canonical_name", "brand", "tags")),
}


def _one_of(column: str, values: Sequence[str]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})")


def _id() -> sa.Column[int]:
    return sa.Column("id", sa.Integer(), primary_key=True)


def _text(name: str, *, nullable: bool = False) -> sa.Column[str]:
    return sa.Column(name, sa.Text(), nullable=nullable)


def _integer(name: str, *, nullable: bool = False) -> sa.Column[int]:
    return sa.Column(name, sa.Integer(), nullable=nullable)


def _reference(name: str, target: str, on_delete: str, *, nullable: bool = False) -> sa.Column[int]:
    return sa.Column(
        name, sa.Integer(), sa.ForeignKey(f"{target}.id", ondelete=on_delete), nullable=nullable
    )


def _create_tables() -> None:
    op.create_table(
        "receipts",
        _id(),
        _text("status"),
        _text("stage"),
        sa.Column("store_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("store_key", sa.Text(), nullable=False, server_default=""),
        _text("purchased_on", nullable=True),
        _text("currency"),
        _integer("subtotal_cents", nullable=True),
        _integer("tax_cents", nullable=True),
        _integer("total_cents", nullable=True),
        _text("payment_last4", nullable=True),
        _text("ocr_text", nullable=True),
        _text("image_path", nullable=True),
        _text("created_at"),
        _text("updated_at"),
        _one_of("status", RECEIPT_STATUSES),
        _one_of("stage", PROCESSING_STAGES),
        sa.CheckConstraint("length(currency) = 3"),
        sa.CheckConstraint("payment_last4 GLOB '[0-9][0-9][0-9][0-9]'"),
    )
    op.create_table(
        "products",
        _id(),
        _text("canonical_name"),
        _text("brand", nullable=True),
        _text("size_value", nullable=True),
        _text("size_unit", nullable=True),
        _text("category", nullable=True),
        sa.Column("tags", sa.Text(), nullable=False, server_default="[]"),
        _text("created_at"),
        _one_of("size_unit", UNITS),
        _one_of("category", CATEGORIES),
    )
    op.create_table(
        "line_items",
        _id(),
        _reference("receipt_id", "receipts", "CASCADE"),
        _integer("position"),
        _text("kind"),
        _text("raw_text"),
        _text("description"),
        _text("quantity"),
        _integer("unit_price_cents", nullable=True),
        _integer("line_total_cents"),
        _reference("product_id", "products", "SET NULL", nullable=True),
        _text("category", nullable=True),
        _one_of("kind", LINE_KINDS),
        _one_of("category", CATEGORIES),
    )
    op.create_table(
        "product_aliases",
        _id(),
        _reference("product_id", "products", "CASCADE"),
        _text("alias"),
        _text("store_key", nullable=True),
        _text("source"),
        _text("created_at"),
        _one_of("source", ALIAS_SOURCES),
    )
    op.create_table(
        "price_observations",
        _id(),
        _reference("product_id", "products", "CASCADE"),
        _reference("receipt_id", "receipts", "CASCADE"),
        _reference("line_item_id", "line_items", "CASCADE"),
        _text("store_key"),
        _text("observed_on"),
        _text("quantity"),
        _integer("unit_price_cents"),
        _integer("line_total_cents"),
        _text("currency"),
    )
    op.create_table(
        "jobs",
        _id(),
        _text("kind"),
        _reference("receipt_id", "receipts", "CASCADE"),
        _text("status"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _integer("max_attempts"),
        _text("available_at"),
        _text("locked_by", nullable=True),
        _text("lease_expires_at", nullable=True),
        _text("last_error", nullable=True),
        _text("created_at"),
        _text("updated_at"),
        _one_of("kind", JOB_KINDS),
        _one_of("status", JOB_STATUSES),
    )
    op.create_table(
        "review_items",
        _id(),
        _reference("receipt_id", "receipts", "CASCADE"),
        _text("reason"),
        _text("details"),
        _text("status"),
        _text("thread_id", nullable=True),
        _text("created_at"),
        _text("resolved_at", nullable=True),
        _one_of("reason", REVIEW_REASONS),
        _one_of("status", REVIEW_STATUSES),
    )
    op.create_table(
        "corrections",
        _id(),
        _text("kind"),
        _text("subject"),
        _text("old_value", nullable=True),
        _text("new_value"),
        _text("store_key", nullable=True),
        _reference("receipt_id", "receipts", "SET NULL", nullable=True),
        _reference("line_item_id", "line_items", "SET NULL", nullable=True),
        _text("created_at"),
        _one_of("kind", CORRECTION_KINDS),
    )


def _create_indexes() -> None:
    newest_first = [sa.text("created_at DESC"), sa.text("id DESC")]
    op.create_index("ix_receipts_newest_first", "receipts", newest_first)
    op.create_index("ix_receipts_status", "receipts", ["status"])
    op.create_index("ix_receipts_store_key", "receipts", ["store_key"])
    op.create_index("ix_receipts_purchased_on", "receipts", ["purchased_on"])
    op.create_index(
        "ux_line_items_receipt_position", "line_items", ["receipt_id", "position"], unique=True
    )
    op.create_index("ix_line_items_product_id", "line_items", ["product_id"])
    # One alias per store, and one global alias: NULL store keys are equal here.
    op.create_index(
        "ux_product_aliases_alias_store",
        "product_aliases",
        ["alias", sa.text("COALESCE(store_key, '')")],
        unique=True,
    )
    op.create_index(
        "ux_price_observations_line_item", "price_observations", ["line_item_id"], unique=True
    )
    op.create_index(
        "ix_price_observations_product_date", "price_observations", ["product_id", "observed_on"]
    )
    op.create_index("ix_jobs_claim_order", "jobs", ["status", "available_at", "id"])
    op.create_index(
        "ux_review_items_open_per_receipt",
        "review_items",
        ["receipt_id"],
        unique=True,
        sqlite_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_corrections_lookup", "corrections", ["kind", "subject", "store_key", "created_at"]
    )
    # Every other foreign key column gets its own index: deleting a receipt, line item or
    # product finds the rows to cascade to (or null out) without scanning the child table.
    for table, column in FOREIGN_KEYS_TO_INDEX:
        op.create_index(f"ix_{table}_{column}", table, [column])


def _create_fts(name: str, content: str, columns: Sequence[str]) -> None:
    """An external-content FTS5 table over ``content`` and the triggers that keep it in step."""
    # Every interpolated name is a constant of this module, never input.
    listed = ", ".join(columns)
    new = ", ".join(f"new.{column}" for column in columns)
    old = ", ".join(f"old.{column}" for column in columns)
    remove = f"INSERT INTO {name}({name}, rowid, {listed}) VALUES ('delete', old.id, {old});"  # noqa: S608
    add = f"INSERT INTO {name}(rowid, {listed}) VALUES (new.id, {new});"  # noqa: S608
    op.execute(
        f"CREATE VIRTUAL TABLE {name} USING fts5({listed},"
        f" content='{content}', content_rowid='id', {FTS_OPTIONS})"
    )
    op.execute(f"CREATE TRIGGER {content}_fts_insert AFTER INSERT ON {content} BEGIN {add} END")
    op.execute(f"CREATE TRIGGER {content}_fts_delete AFTER DELETE ON {content} BEGIN {remove} END")
    op.execute(
        f"CREATE TRIGGER {content}_fts_update AFTER UPDATE OF {listed} ON {content}"
        f" BEGIN {remove} {add} END"
    )


def _create_vectors() -> None:
    op.execute(
        "CREATE VIRTUAL TABLE product_vectors"
        f" USING vec0(embedding float[{EMBEDDING_DIMENSION}] distance_metric=cosine)"
    )
    op.execute(
        "CREATE TRIGGER products_vector_delete AFTER DELETE ON products"
        " BEGIN DELETE FROM product_vectors WHERE rowid = old.id; END"
    )


def upgrade() -> None:
    _create_tables()
    _create_indexes()
    for name, (content, columns) in FTS_TABLES.items():
        _create_fts(name, content, columns)
    _create_vectors()


def downgrade() -> None:
    # Dropping a table drops its indexes and triggers; virtual tables drop their shadow tables.
    for virtual in ("product_vectors", *FTS_TABLES):
        op.execute(f"DROP TABLE {virtual}")
    for table in (
        "corrections",
        "review_items",
        "jobs",
        "price_observations",
        "product_aliases",
        "line_items",
        "products",
        "receipts",
    ):
        op.drop_table(table)
