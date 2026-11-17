"""Review answers from the web app: a ``resolve_review`` job carries a person's answer to a
review to the worker, which resumes the receipt's graph with it.

``jobs.kind`` gains ``resolve_review`` and ``jobs`` gains ``payload`` (JSON text, NULL for a
``process_receipt`` job). The kind's CHECK is a table constraint, which SQLite cannot alter, so
the table is rebuilt: the old one renamed, the new one created, the rows copied, the old one
dropped (with its indexes) and the two indexes created again. Nothing references ``jobs``.

The downgrade refuses, before any change, while a ``resolve_review`` job exists, naming how
many; otherwise it rebuilds the table as 0008 had it.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_KINDS_0008 = ("process_receipt",)
JOB_KINDS = ("process_receipt", "resolve_review")
JOB_STATUSES = ("pending", "running", "succeeded", "failed")
COLUMNS_0008 = (
    "id, kind, receipt_id, status, attempts, max_attempts, available_at, locked_by,"
    " lease_expires_at, last_error, created_at, updated_at"
)


def _one_of(column: str, values: Sequence[str]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})")


def _text(name: str, *, nullable: bool = False) -> sa.Column[str]:
    return sa.Column(name, sa.Text(), nullable=nullable)


def _rebuild_jobs(kinds: Sequence[str], *, payload: bool) -> None:
    """``jobs`` again, as 0002 created it but with ``kinds`` (and ``payload`` last), every row
    kept. The old table is renamed out of the way first, so the new one is created under its
    own name and its schema text is what a fresh ``CREATE TABLE`` writes."""
    op.execute("ALTER TABLE jobs RENAME TO jobs_0009")
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        _text("kind"),
        sa.Column(
            "receipt_id",
            sa.Integer(),
            sa.ForeignKey("receipts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _text("status"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        _text("available_at"),
        _text("locked_by", nullable=True),
        _text("lease_expires_at", nullable=True),
        _text("last_error", nullable=True),
        _text("created_at"),
        _text("updated_at"),
        *([_text("payload", nullable=True)] if payload else []),
        _one_of("kind", kinds),
        _one_of("status", JOB_STATUSES),
    )
    op.execute(f"INSERT INTO jobs ({COLUMNS_0008}) SELECT {COLUMNS_0008} FROM jobs_0009")  # noqa: S608
    op.execute("DROP TABLE jobs_0009")
    op.create_index("ix_jobs_claim_order", "jobs", ["status", "available_at", "id"])
    op.create_index("ix_jobs_receipt_id", "jobs", ["receipt_id"])


def upgrade() -> None:
    _rebuild_jobs(JOB_KINDS, payload=True)


def downgrade() -> None:
    answers = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM jobs WHERE kind = 'resolve_review'"))
        .scalar_one()
    )
    if answers:
        msg = (
            f"{answers} jobs are resolve_review jobs, which 0008 cannot store;"
            " let the worker run them (or delete them), then downgrade again"
        )
        raise RuntimeError(msg)
    _rebuild_jobs(JOB_KINDS_0008, payload=False)
