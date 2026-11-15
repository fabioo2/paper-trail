"""Foreign key indexes: the six lookup indexes of 0002's foreign key columns, for a database
that applied 0002 before they were added to it (a fresh database already has them).

Each is created only when its name is free, then checked: an index of that name on another
table, on other columns, descending, with another collation, unique or partial stops the upgrade
with its name, since the schema would not be a fresh database's. The downgrade keeps them: 0002
owns them on a fresh database.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOREIGN_KEYS_TO_INDEX = (
    ("product_aliases", "product_id"),
    ("price_observations", "receipt_id"),
    ("jobs", "receipt_id"),
    ("review_items", "receipt_id"),
    ("corrections", "receipt_id"),
    ("corrections", "line_item_id"),
)


def upgrade() -> None:
    connection = op.get_bind()
    for table, column in FOREIGN_KEYS_TO_INDEX:
        name = f"ix_{table}_{column}"
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})")
        indexes = connection.execute(sa.text(f"PRAGMA index_list({table})")).mappings().all()
        found = next((index for index in indexes if index["name"] == name), None)
        keys = [
            entry
            for entry in connection.execute(sa.text(f"PRAGMA index_xinfo({name})")).mappings()
            if entry["key"]
        ]
        if (
            found is None
            or found["unique"]
            or found["partial"]
            or [(key["name"], key["desc"], key["coll"]) for key in keys] != [(column, 0, "BINARY")]
        ):
            msg = (
                f"index {name} exists but is not a plain index of {table} ({column});"
                " drop it, then upgrade again"
            )
            raise RuntimeError(msg)


def downgrade() -> None:
    pass
