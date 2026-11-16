"""SQLite engine: pragmas and FTS5 on every connection (S0-AC-22); no sqlite-vec, which only the
migration runner loads (CE-TC-050)."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from paper_trail.adapters.sqlite.engine import create_engine

EXPECTED_PRAGMAS = {
    "journal_mode": "wal",
    "foreign_keys": 1,
    "busy_timeout": 5000,
    "synchronous": 1,  # NORMAL
}


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(tmp_path / "nested" / "paper_trail.db")
    yield engine
    engine.dispose()


def test_s0_ac_22_creates_the_parent_directory(tmp_path: Path, engine: Engine) -> None:
    """S0-AC-22: create_engine works on an empty data volume (parent dir created)."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    assert (tmp_path / "nested" / "paper_trail.db").is_file()


def test_s0_ac_22_uses_the_pysqlite_driver(engine: Engine) -> None:
    """S0-AC-22: URL is sqlite+pysqlite:///<path>."""
    assert engine.url.drivername == "sqlite+pysqlite"


def test_s0_ac_22_every_connection_gets_the_pragmas(engine: Engine) -> None:
    """S0-AC-22: pragmas are set on every new pooled connection, not only the first."""
    connections = [engine.connect() for _ in range(3)]
    try:
        for conn in connections:
            for pragma, expected in EXPECTED_PRAGMAS.items():
                assert conn.execute(text(f"PRAGMA {pragma}")).scalar_one() == expected, pragma
    finally:
        for conn in connections:
            conn.close()


def test_ce_tc_050_the_engine_does_not_load_sqlite_vec(engine: Engine) -> None:
    for _ in range(2):
        with engine.connect() as conn, pytest.raises(OperationalError, match="no such function"):
            conn.execute(text("SELECT vec_version()"))


def test_s0_ac_22_extension_loading_is_disabled_after_connect(engine: Engine) -> None:
    """S0-AC-22: extension loading stays disabled on application connections."""
    with engine.connect() as conn, pytest.raises(OperationalError, match="not authorized"):
        conn.execute(text("SELECT load_extension('/nonexistent/evil')"))


def test_s0_ac_22_fts5_is_available(engine: Engine) -> None:
    """S0-AC-22: CREATE VIRTUAL TABLE t USING fts5(x) succeeds and matches."""
    with engine.begin() as conn:
        conn.execute(text("CREATE VIRTUAL TABLE temp.t USING fts5(x)"))
        conn.execute(text("INSERT INTO temp.t(x) VALUES ('kirkland organic eggs')"))
        hits = conn.execute(text("SELECT count(*) FROM temp.t WHERE t MATCH 'eggs'")).scalar_one()

    assert hits == 1


def test_s0_ac_22_connections_are_usable_across_threads(engine: Engine) -> None:
    """S0-AC-22: check_same_thread=False so the pool can hand connections to worker threads."""

    errors: list[BaseException] = []

    def query() -> None:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except BaseException as exc:
            errors.append(exc)

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    thread = threading.Thread(target=query)
    thread.start()
    thread.join()

    assert errors == []
