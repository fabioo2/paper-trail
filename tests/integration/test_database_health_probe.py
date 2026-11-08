"""DatabaseHealthProbe against real SQLite files (S0-AC-18)."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import Engine

from paper_trail.adapters.sqlite import health as health_module
from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.health import DatabaseHealthProbe
from paper_trail.adapters.sqlite.migrations import upgrade_to_head
from paper_trail.domain.health import HealthStatus
from tests.support import SCHEMA_HEAD

DATA_KEYS = {"revision", "head", "sqlite_version", "sqlite_vec"}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(tmp_path / "paper_trail.db")
    yield engine
    engine.dispose()


def test_s0_ac_18_probe_is_named_database(engine: Engine) -> None:
    """S0-AC-18: the component is reported as "database"."""
    assert DatabaseHealthProbe(engine).name == "database"


def test_s0_ac_18_ok_at_head_with_component_data(engine: Engine) -> None:
    """S0-AC-18: at head the probe is OK and reports revision, head, sqlite and vec versions."""
    upgrade_to_head(engine)

    health = DatabaseHealthProbe(engine).check()

    assert health.status is HealthStatus.OK
    assert health.detail is None
    assert health.data.keys() >= DATA_KEYS
    assert health.data["revision"] == health.data["head"] == SCHEMA_HEAD
    assert health.data["sqlite_version"] == sqlite3.sqlite_version
    assert health.data["sqlite_vec"].startswith("v")


def test_s0_ac_18_error_when_not_migrated(engine: Engine) -> None:
    """S0-AC-18: an unmigrated database is an ERROR with a human-readable detail."""
    with engine.connect():
        pass  # creates the empty database file
    health = DatabaseHealthProbe(engine).check()

    assert health.status is HealthStatus.ERROR
    assert health.detail
    assert health.data.get("head") == SCHEMA_HEAD


def test_s0_ac_18_error_not_exception_when_unreadable(tmp_path: Path) -> None:
    """S0-AC-18: check() must not raise; an unreadable file becomes status ERROR."""
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"definitely not sqlite" * 100)
    engine = create_engine(corrupt)
    try:
        health = DatabaseHealthProbe(engine).check()
    finally:
        engine.dispose()

    assert health.status is HealthStatus.ERROR
    assert health.detail


def test_s0_ac_18_error_when_path_is_a_directory(tmp_path: Path) -> None:
    """S0-AC-18: a database path that cannot be opened is ERROR, not a crash."""
    directory = tmp_path / "is-a-directory.db"
    directory.mkdir()
    engine = create_engine(directory)
    try:
        health = DatabaseHealthProbe(engine).check()
    finally:
        engine.dispose()

    assert health.status is HealthStatus.ERROR


def test_s0_ac_18_missing_database_is_error_and_is_not_created(tmp_path: Path) -> None:
    """S0-AC-18: probing has no side effects: a missing file is ERROR and stays missing."""
    database = tmp_path / "data" / "paper_trail.db"
    engine = create_engine(database)
    try:
        health = DatabaseHealthProbe(engine).check()
    finally:
        engine.dispose()

    assert health.status is HealthStatus.ERROR
    assert health.detail
    assert "db upgrade" in health.detail
    assert health.data == {"head": SCHEMA_HEAD}
    assert not database.parent.exists(), "the probe must not create the data directory"


def test_s0_ac_18_unreadable_migrations_are_error_not_exception(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S0-AC-18: a broken migrations directory is reported as ERROR; check() never raises."""

    def broken_head() -> str:
        msg = "no Alembic revisions found"
        raise RuntimeError(msg)

    upgrade_to_head(engine)
    monkeypatch.setattr(health_module, "head_revision", broken_head)

    health = DatabaseHealthProbe(engine).check()

    assert health.status is HealthStatus.ERROR
    assert health.detail


def test_s0_ac_18_probe_requires_a_file_database() -> None:
    """S0-AC-18: an in-memory engine is a wiring error, caught at construction."""
    engine = sqlalchemy.create_engine("sqlite+pysqlite:///:memory:")
    try:
        with pytest.raises(ValueError, match="database file"):
            DatabaseHealthProbe(engine)
    finally:
        engine.dispose()


def test_s1_ac_09_ok_reports_the_vector_dimension(engine: Engine) -> None:
    """PLAN 3.6: readiness also checks that product_vectors holds 768-d embeddings."""
    upgrade_to_head(engine)

    health = DatabaseHealthProbe(engine).check()

    assert health.status is HealthStatus.OK
    assert health.data["vector_dimension"] == "768"


def test_s1_ac_09_error_when_the_vector_dimension_is_wrong(engine: Engine) -> None:
    """PLAN 3.6: a vec0 table of another dimension (e.g. a different embedding model) is ERROR."""
    upgrade_to_head(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE product_vectors")
        connection.exec_driver_sql(
            "CREATE VIRTUAL TABLE product_vectors"
            " USING vec0(embedding float[384] distance_metric=cosine)"
        )

    health = DatabaseHealthProbe(engine).check()

    assert health.status is HealthStatus.ERROR
    assert health.detail
    assert "768" in health.detail
