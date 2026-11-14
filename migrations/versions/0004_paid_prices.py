"""Paid prices: a discount line names the item it reduces, and each price observation keeps
its regular price, its discount and what was paid (discounts-and-paid-prices plan, section 6).

``line_items.applies_to_position`` is added in place (the FTS triggers and incoming foreign keys
stay); ``price_observations`` is rebuilt with ``discount_cents``, ``unit_discount_cents``,
``paid_cents`` and ``on_sale``, existing rows copied with no discount. Nothing references
``price_observations`` and it has no triggers, so the rebuild touches nothing else; its indexes
are recreated under their 0002 names. An observation with a negative line total cannot satisfy
``paid_cents >= 0``, so the upgrade refuses before any change when one exists.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS_0002 = """
    id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    receipt_id INTEGER NOT NULL,
    line_item_id INTEGER NOT NULL,
    store_key TEXT NOT NULL,
    observed_on TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    line_total_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,"""
PAID_COLUMNS = """
    discount_cents INTEGER NOT NULL DEFAULT 0 CHECK (discount_cents <= 0),
    unit_discount_cents INTEGER NOT NULL DEFAULT 0 CHECK (unit_discount_cents <= 0),
    paid_cents INTEGER NOT NULL CHECK (paid_cents >= 0),
    on_sale BOOLEAN NOT NULL DEFAULT 0 CHECK (on_sale IN (0, 1)),
    CHECK (paid_cents = line_total_cents + discount_cents),
    CHECK (on_sale = (discount_cents < 0)),"""
KEYS_0002 = """
    PRIMARY KEY (id),
    FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE CASCADE,
    FOREIGN KEY(receipt_id) REFERENCES receipts (id) ON DELETE CASCADE,
    FOREIGN KEY(line_item_id) REFERENCES line_items (id) ON DELETE CASCADE"""
COPIED = (
    "id, product_id, receipt_id, line_item_id, store_key, observed_on, quantity,"
    " unit_price_cents, line_total_cents, currency"
)


def upgrade() -> None:
    negative = (
        op.get_bind()
        .execute(
            sa.text("SELECT id FROM price_observations WHERE line_total_cents < 0 ORDER BY id")
        )
        .scalars()
        .all()
    )
    if negative:
        listed = ", ".join(str(observation_id) for observation_id in negative)
        msg = (
            f"price observations with a negative line total cannot get a paid price: {listed};"
            " delete or correct them, then upgrade again"
        )
        raise RuntimeError(msg)
    op.execute(
        "ALTER TABLE line_items ADD COLUMN applies_to_position INTEGER CHECK ("
        "applies_to_position IS NULL OR (kind = 'discount' AND applies_to_position >= 0"
        " AND applies_to_position <> position))"
    )
    _rebuild_price_observations(
        COLUMNS_0002 + PAID_COLUMNS,
        f"{COPIED}, discount_cents, unit_discount_cents, paid_cents, on_sale",
        f"{COPIED}, 0, 0, line_total_cents, 0",
    )


def downgrade() -> None:
    _rebuild_price_observations(COLUMNS_0002, COPIED, COPIED)
    op.execute("ALTER TABLE line_items DROP COLUMN applies_to_position")


def _rebuild_price_observations(columns: str, into: str, select: str) -> None:
    """Recreate ``price_observations`` with ``columns``, copying every row, and its indexes."""
    op.execute(f"CREATE TABLE price_observations_new ({columns}{KEYS_0002}\n)")
    op.execute(
        f"INSERT INTO price_observations_new ({into}) SELECT {select} FROM price_observations"  # noqa: S608
    )
    op.execute("DROP TABLE price_observations")
    op.execute("ALTER TABLE price_observations_new RENAME TO price_observations")
    op.create_index(
        "ux_price_observations_line_item", "price_observations", ["line_item_id"], unique=True
    )
    op.create_index(
        "ix_price_observations_product_date", "price_observations", ["product_id", "observed_on"]
    )
    op.create_index("ix_price_observations_receipt_id", "price_observations", ["receipt_id"])
