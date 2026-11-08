"""Alembic wiring and the baseline migration (S0-AC-23)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import Engine, inspect

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import (
    alembic_config,
    current_revision,
    head_revision,
    upgrade_to_head,
)
from tests.support import SCHEMA_HEAD


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "paper_trail.db"


@pytest.fixture
def engine(database: Path) -> Iterator[Engine]:
    engine = create_engine(database)
    yield engine
    engine.dispose()


def test_s0_ac_23_head_is_the_newest_revision() -> None:
    """S0-AC-23: head_revision() names the newest migration (0002 since Stage 1)."""
    assert head_revision() == SCHEMA_HEAD


def test_s0_ac_23_empty_database_has_no_revision(engine: Engine) -> None:
    """S0-AC-23: a brand-new file reports no current revision."""
    assert current_revision(engine) is None


def test_s0_ac_23_upgrade_sets_the_head_revision(engine: Engine) -> None:
    """S0-AC-23: upgrade_to_head on an empty file sets the head revision."""
    upgrade_to_head(engine)

    assert current_revision(engine) == SCHEMA_HEAD


def test_s0_ac_23_upgrade_is_idempotent(engine: Engine) -> None:
    """S0-AC-23: running upgrade twice is a no-op."""
    upgrade_to_head(engine)
    tables_before = set(inspect(engine).get_table_names())

    upgrade_to_head(engine)

    assert current_revision(engine) == SCHEMA_HEAD
    assert set(inspect(engine).get_table_names()) == tables_before


def test_s0_ac_23_baseline_is_empty(database: Path, engine: Engine) -> None:
    """S0-AC-23: the baseline migration (0001) creates no application tables."""
    command.upgrade(alembic_config(database), "0001")

    assert set(inspect(engine).get_table_names()) == {"alembic_version"}


def test_s0_ac_23_downgrade_base_then_upgrade_head(database: Path, engine: Engine) -> None:
    """S0-AC-23: `alembic downgrade base` then `upgrade head` succeeds."""
    config = alembic_config(database)

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert current_revision(engine) is None

    command.upgrade(config, "head")
    assert current_revision(engine) == SCHEMA_HEAD


def test_s0_ac_23_alembic_url_comes_from_the_given_path(tmp_path: Path) -> None:
    """S0-AC-23: the Alembic URL is built from the configured path, never from alembic.ini."""
    database = tmp_path / "elsewhere" / "custom.db"

    command.upgrade(alembic_config(database), "head")

    assert database.is_file()
    probe = create_engine(database)
    try:
        assert current_revision(probe) == SCHEMA_HEAD
    finally:
        probe.dispose()


def test_s0_ac_23_migrations_run_with_the_engine_pragmas(database: Path, engine: Engine) -> None:
    """S0-AC-23: migrations use create_engine, so later ones can rely on sqlite-vec and FTS5."""
    upgrade_to_head(engine)

    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
