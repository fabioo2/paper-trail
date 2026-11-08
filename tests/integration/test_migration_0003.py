"""Revision 0003, the agent core's columns, and a clean downgrade (PLAN 3.5, AC-A-02).

``receipts.image_sha256`` (TEXT NULL, unique index) backs image dedup; ``line_items.match_source``
(TEXT NULL, CHECK in ``MatchSource``) records how a line was matched (slice C). LangGraph's
``SqliteSaver`` tables are created by the saver in the same file, never by a migration.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import alembic_config, current_revision
from tests.support import SCHEMA_HEAD

NOW = "2026-11-06T15:00:00.000000+00:00"


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "paper_trail.db"


@pytest.fixture
def engine(database: Path) -> Iterator[Engine]:
    engine = create_engine(database)
    yield engine
    engine.dispose()


def _columns(connection: Connection, table: str) -> dict[str, tuple[str, bool]]:
    rows = connection.exec_driver_sql(f'PRAGMA table_info("{table}")').mappings()
    return {row["name"]: (row["type"].upper(), not row["notnull"]) for row in rows}


def _unique_single_column_indexes(connection: Connection, table: str) -> set[str]:
    found = set()
    for row in connection.exec_driver_sql(f'PRAGMA index_list("{table}")').mappings():
        if not row["unique"] or row["partial"]:
            continue
        columns = [
            info["name"]
            for info in connection.exec_driver_sql(f'PRAGMA index_info("{row["name"]}")').mappings()
        ]
        if len(columns) == 1:
            found.add(columns[0])
    return found


def _insert_receipt(connection: Connection, sha: str | None) -> None:
    connection.execute(
        text(
            "INSERT INTO receipts (status, stage, currency, image_sha256, created_at, updated_at)"
            " VALUES ('processing', 'reading', 'CAD', :sha, :now, :now)"
        ),
        {"sha": sha, "now": NOW},
    )


def test_head_is_0003() -> None:
    assert SCHEMA_HEAD == "0003"


def test_upgrade_adds_the_agent_core_columns(database: Path, engine: Engine) -> None:
    command.upgrade(alembic_config(database), "head")

    with engine.connect() as connection:
        assert _columns(connection, "receipts")["image_sha256"] == ("TEXT", True)
        assert _columns(connection, "line_items")["match_source"] == ("TEXT", True)
        assert "image_sha256" in _unique_single_column_indexes(connection, "receipts")
    assert current_revision(engine) == "0003"


def test_one_receipt_per_image_and_many_without(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as connection:
        _insert_receipt(connection, "a" * 64)
        _insert_receipt(connection, None)
        _insert_receipt(connection, None)

    with pytest.raises(IntegrityError), migrated_engine.begin() as connection:
        _insert_receipt(connection, "a" * 64)


def test_downgrade_to_0002_removes_only_the_new_columns(database: Path, engine: Engine) -> None:
    config = alembic_config(database)
    command.upgrade(config, "head")
    with engine.begin() as connection:
        _insert_receipt(connection, "b" * 64)

    command.downgrade(config, "0002")

    assert current_revision(engine) == "0002"
    with engine.connect() as connection:
        assert "image_sha256" not in _columns(connection, "receipts")
        assert "match_source" not in _columns(connection, "line_items")
        assert connection.execute(text("SELECT count(*) FROM receipts")).scalar_one() == 1
    command.upgrade(config, "head")
    assert current_revision(engine) == SCHEMA_HEAD
