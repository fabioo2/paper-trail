"""Baseline: an empty schema that later revisions build on.

Revision ID: 0001
Revises:
Create Date: 2026-11-06
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to create yet; Stage 1 adds the core schema in revision 0002."""


def downgrade() -> None:
    """Nothing to drop."""
