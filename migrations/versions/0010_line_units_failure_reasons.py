"""Line units and failure reasons (hardening plan 2, 3).

``line_items.unit`` records what an item line is sold by (``each``, ``kg`` or ``lb``), so a
product's prices are compared per the same unit; existing lines get NULL (unknown, compared as
``each``). ``receipts.failure_reason`` records why a FAILED receipt failed, as one of a few
words a person can act on; existing FAILED receipts get NULL (read as ``unknown``). Both are
added in place, so the FTS triggers, indexes and incoming foreign keys stay.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINE_UNITS = ("each", "kg", "lb")
FAILURE_REASONS = (
    "budget_reached",
    "claude_unavailable",
    "no_api_key",
    "unreadable_photo",
    "stopped_unexpectedly",
    "unknown",
)


def _allowed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE line_items ADD COLUMN unit TEXT CHECK (unit IN ({_allowed(LINE_UNITS)}))"
    )
    op.execute(
        "ALTER TABLE receipts ADD COLUMN failure_reason TEXT CHECK"
        f" (failure_reason IN ({_allowed(FAILURE_REASONS)}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE receipts DROP COLUMN failure_reason")
    op.execute("ALTER TABLE line_items DROP COLUMN unit")
