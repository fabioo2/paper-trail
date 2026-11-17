"""Revision 0009, review answers: ``jobs.kind`` takes ``resolve_review`` and ``jobs`` gains
``payload``.

A real SQLite file in ``tmp_path``, seeded at 0008 with raw SQL: a receipt and two
``process_receipt`` jobs (one running, one failed).

- Upgrade: every job and value as it was, ``payload`` NULL; a ``resolve_review`` job with a
  payload is stored; any other kind is refused; deleting the receipt still deletes its jobs.
- Downgrade: refused, changing nothing, while a ``resolve_review`` job exists; otherwise the
  schema text and the rows are 0008's again.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, text

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import alembic_config, current_revision
from tests.support import SCHEMA_HEAD

NOW = "2026-11-16T12:00:00.000000+00:00"
JOB_COLUMNS = (
    "id, kind, receipt_id, status, attempts, max_attempts, available_at, locked_by,"
    " lease_expires_at, last_error, created_at, updated_at"
)
SEEDED_JOBS = [
    (1, "process_receipt", 1, "running", 0, 1, NOW, "worker-1", NOW, None, NOW, NOW),
    (2, "process_receipt", 1, "failed", 1, 1, NOW, None, None, "Claude is down", NOW, NOW),
]
INSERT_JOB = text(
    "INSERT INTO jobs (kind, receipt_id, status, max_attempts, available_at, created_at,"
    " updated_at, payload) VALUES (:kind, 1, 'pending', 1, :now, :now, :now, :payload)"
)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "paper_trail.db"


@pytest.fixture
def engine(database: Path) -> Iterator[Engine]:
    engine = create_engine(database)
    yield engine
    engine.dispose()


def _schema(database: Path) -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = 'jobs' ORDER BY name"
        ).fetchall()


def _jobs(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        rows = connection.execute(text(f"SELECT {JOB_COLUMNS} FROM jobs ORDER BY id"))  # noqa: S608
        return [tuple(row) for row in rows]


def _seed_0008(database: Path, engine: Engine) -> list[tuple[str, str, str]]:
    command.upgrade(alembic_config(database), "0008")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO receipts (id, status, stage, store_name, store_key, currency,"
                " created_at, updated_at) VALUES (1, 'processing', 'reading', '', '', 'CAD',"
                " :now, :now)"
            ),
            {"now": NOW},
        )
        for job in SEEDED_JOBS:
            connection.execute(
                text(
                    f"INSERT INTO jobs ({JOB_COLUMNS}) VALUES (:id, :kind, :receipt_id, :status,"  # noqa: S608
                    " :attempts, :max_attempts, :available_at, :locked_by, :lease_expires_at,"
                    " :last_error, :created_at, :updated_at)"
                ),
                dict(zip(JOB_COLUMNS.replace(" ", "").split(","), job, strict=True)),
            )
    return _schema(database)


def test_upgrade_keeps_every_job_and_takes_review_answers(database: Path, engine: Engine) -> None:
    _seed_0008(database, engine)

    command.upgrade(alembic_config(database), "head")

    assert current_revision(engine) == SCHEMA_HEAD
    assert _jobs(engine) == SEEDED_JOBS
    with engine.begin() as connection:
        connection.execute(
            INSERT_JOB, {"kind": "resolve_review", "now": NOW, "payload": '{"review_id": 1}'}
        )
        payloads = connection.execute(text("SELECT payload FROM jobs ORDER BY id")).scalars()
        assert list(payloads) == [None, None, '{"review_id": 1}']
    with pytest.raises(Exception, match="constraint failed"), engine.begin() as connection:
        connection.execute(INSERT_JOB, {"kind": "reprocess", "now": NOW, "payload": None})
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM receipts WHERE id = 1"))
    assert _jobs(engine) == []


def test_downgrade_refuses_while_an_answer_is_queued_then_gives_back_0008(
    database: Path, engine: Engine
) -> None:
    schema_0008 = _seed_0008(database, engine)
    config = alembic_config(database)
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(INSERT_JOB, {"kind": "resolve_review", "now": NOW, "payload": "{}"})

    with pytest.raises(RuntimeError, match="1 jobs are resolve_review jobs"):
        command.downgrade(config, "0008")
    assert current_revision(engine) == SCHEMA_HEAD

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM jobs WHERE kind = 'resolve_review'"))
    command.downgrade(config, "0008")

    assert current_revision(engine) == "0008"
    assert _schema(database) == schema_0008
    assert _jobs(engine) == SEEDED_JOBS
