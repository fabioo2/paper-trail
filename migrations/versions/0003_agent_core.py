"""Agent core: one receipt per image, and how each line was matched.

``receipts.image_sha256`` (unique; NULL for receipts without an image) backs image
deduplication; ``line_items.match_source`` records which matching step linked the line (agent
core slice C). LangGraph's checkpoint tables are created by its SqliteSaver in the same file,
never by a migration.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MATCH_SOURCES = ("memory", "alias", "vector", "llm", "new")


def upgrade() -> None:
    allowed = ", ".join(f"'{value}'" for value in MATCH_SOURCES)
    op.execute("ALTER TABLE receipts ADD COLUMN image_sha256 TEXT")
    op.create_index("ux_receipts_image_sha256", "receipts", ["image_sha256"], unique=True)
    op.execute(
        f"ALTER TABLE line_items ADD COLUMN match_source TEXT CHECK (match_source IN ({allowed}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE line_items DROP COLUMN match_source")
    op.drop_index("ux_receipts_image_sha256", table_name="receipts")
    op.execute("ALTER TABLE receipts DROP COLUMN image_sha256")
