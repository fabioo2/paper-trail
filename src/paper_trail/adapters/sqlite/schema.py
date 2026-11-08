"""SQLAlchemy Core tables of the regular (non-virtual) tables of the schema (PLAN 3.5).

The migrations own the schema: tables, indexes, CHECK constraints, triggers and the FTS5 and
sqlite-vec virtual tables. This module mirrors the regular tables for queries, and a test
(``tests/integration/test_schema_matches_metadata.py``) keeps both in step: columns, types,
nullability and foreign keys must match the migrated database.
"""

from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table, Text

from paper_trail.adapters.sqlite.types import (
    DecimalText,
    IsoDate,
    JsonText,
    StrEnumType,
    UtcDateTime,
)
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

metadata = MetaData()

receipts = Table(
    "receipts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("status", StrEnumType(ReceiptStatus), nullable=False),
    Column("stage", StrEnumType(ProcessingStage), nullable=False),
    Column("store_name", Text, nullable=False, server_default=""),
    Column("store_key", Text, nullable=False, server_default=""),
    Column("purchased_on", IsoDate, nullable=True),
    Column("currency", Text, nullable=False),
    Column("subtotal_cents", Integer, nullable=True),
    Column("tax_cents", Integer, nullable=True),
    Column("total_cents", Integer, nullable=True),
    Column("payment_last4", Text, nullable=True),
    Column("ocr_text", Text, nullable=True),
    Column("image_path", Text, nullable=True),
    Column("created_at", UtcDateTime, nullable=False),
    Column("updated_at", UtcDateTime, nullable=False),
    Column("image_sha256", Text, nullable=True),
)

products = Table(
    "products",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("canonical_name", Text, nullable=False),
    Column("brand", Text, nullable=True),
    Column("size_value", DecimalText, nullable=True),
    Column("size_unit", StrEnumType(Unit), nullable=True),
    Column("category", StrEnumType(Category), nullable=True),
    Column("tags", JsonText, nullable=False, server_default="[]"),
    Column("created_at", UtcDateTime, nullable=False),
)

line_items = Table(
    "line_items",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("receipt_id", Integer, ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False),
    Column("position", Integer, nullable=False),
    Column("kind", StrEnumType(LineKind), nullable=False),
    Column("raw_text", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("quantity", DecimalText, nullable=False),
    Column("unit_price_cents", Integer, nullable=True),
    Column("line_total_cents", Integer, nullable=False),
    Column("product_id", Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True),
    Column("category", StrEnumType(Category), nullable=True),
    Column("match_source", StrEnumType(MatchSource), nullable=True),
)

product_aliases = Table(
    "product_aliases",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("product_id", Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
    Column("alias", Text, nullable=False),
    Column("store_key", Text, nullable=True),
    Column("source", StrEnumType(AliasSource), nullable=False),
    Column("created_at", UtcDateTime, nullable=False),
)

price_observations = Table(
    "price_observations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("product_id", Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
    Column("receipt_id", Integer, ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False),
    Column(
        "line_item_id", Integer, ForeignKey("line_items.id", ondelete="CASCADE"), nullable=False
    ),
    Column("store_key", Text, nullable=False),
    Column("observed_on", IsoDate, nullable=False),
    Column("quantity", DecimalText, nullable=False),
    Column("unit_price_cents", Integer, nullable=False),
    Column("line_total_cents", Integer, nullable=False),
    Column("currency", Text, nullable=False),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", StrEnumType(JobKind), nullable=False),
    Column("receipt_id", Integer, ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False),
    Column("status", StrEnumType(JobStatus), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False),
    Column("available_at", UtcDateTime, nullable=False),
    Column("locked_by", Text, nullable=True),
    Column("lease_expires_at", UtcDateTime, nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_at", UtcDateTime, nullable=False),
    Column("updated_at", UtcDateTime, nullable=False),
)

review_items = Table(
    "review_items",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("receipt_id", Integer, ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False),
    Column("reason", StrEnumType(ReviewReason), nullable=False),
    Column("details", JsonText, nullable=False),
    Column("status", StrEnumType(ReviewStatus), nullable=False),
    Column("thread_id", Text, nullable=True),
    Column("created_at", UtcDateTime, nullable=False),
    Column("resolved_at", UtcDateTime, nullable=True),
)

corrections = Table(
    "corrections",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", StrEnumType(CorrectionKind), nullable=False),
    Column("subject", Text, nullable=False),
    Column("old_value", Text, nullable=True),
    Column("new_value", Text, nullable=False),
    Column("store_key", Text, nullable=True),
    Column("receipt_id", Integer, ForeignKey("receipts.id", ondelete="SET NULL"), nullable=True),
    Column(
        "line_item_id", Integer, ForeignKey("line_items.id", ondelete="SET NULL"), nullable=True
    ),
    Column("created_at", UtcDateTime, nullable=False),
)
