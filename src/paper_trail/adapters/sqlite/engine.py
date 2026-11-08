"""The SQLite engine: one file, WAL mode, sqlite-vec and FTS5 on every connection (ADR-0004).

Transactions are begun explicitly by SQLAlchemy, not implicitly by the sqlite3 driver, so a
write transaction can take SQLite's write lock up front with ``BEGIN IMMEDIATE``
(``begin_immediate``). A deferred transaction that reads and then writes can fail at once with
"database is locked" when another connection wrote in between; an immediate one waits for the
lock (``busy_timeout``) instead. A read transaction (``begin_read_only``) stays deferred, so in
WAL mode it reads its snapshot while a writer holds the lock, and runs with ``query_only`` so a
write through it fails instead of silently taking the lock.
"""

import sqlite3
from pathlib import Path
from typing import Any

import sqlalchemy
import sqlite_vec
from sqlalchemy import URL, Connection, Engine, RootTransaction, event
from sqlalchemy.engine import Dialect
from sqlalchemy.pool import ConnectionPoolEntry

DRIVER = "sqlite+pysqlite"

# Connection execution option naming the BEGIN mode of the connection's next transaction.
_BEGIN_MODE = "paper_trail_begin_mode"
_DEFERRED = "DEFERRED"
_IMMEDIATE = "IMMEDIATE"
_READ_ONLY = "READ_ONLY"

# The statements that begin a transaction in each mode. query_only is a connection setting that
# outlives the transaction, so every mode sets it: a pooled connection never inherits it.
_BEGIN_STATEMENTS = {
    _DEFERRED: ("PRAGMA query_only=OFF", "BEGIN DEFERRED"),
    _IMMEDIATE: ("PRAGMA query_only=OFF", "BEGIN IMMEDIATE"),
    _READ_ONLY: ("PRAGMA query_only=ON", "BEGIN DEFERRED"),
}

CONNECTION_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)


def create_engine(database_path: Path) -> Engine:
    """Build a lazily connecting engine for ``database_path``.

    Nothing touches the filesystem until the first connection, which creates the parent
    directory (an empty data volume works) and configures the connection.
    """
    engine = sqlalchemy.create_engine(
        URL.create(DRIVER, database=str(database_path)),
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "do_connect", _create_parent_directory)
    event.listen(engine, "connect", _configure_connection)
    event.listen(engine, "begin", _begin)
    return engine


def begin_immediate(connection: Connection) -> RootTransaction:
    """Begin a write transaction on ``connection`` that holds the write lock from the start."""
    return _begin_in_mode(connection, _IMMEDIATE)


def begin_read_only(connection: Connection) -> RootTransaction:
    """Begin a read transaction on ``connection``: no write lock, and writing is an error."""
    return _begin_in_mode(connection, _READ_ONLY)


def _begin_in_mode(connection: Connection, mode: str) -> RootTransaction:
    connection.execution_options(**{_BEGIN_MODE: mode})
    return connection.begin()


def _begin(connection: Connection) -> None:
    mode = connection.get_execution_options().get(_BEGIN_MODE, _DEFERRED)
    for statement in _BEGIN_STATEMENTS[mode]:
        connection.exec_driver_sql(statement)


def _create_parent_directory(
    _dialect: Dialect,
    _record: ConnectionPoolEntry,
    cargs: tuple[Any, ...],
    _cparams: dict[str, Any],
) -> None:
    Path(cargs[0]).parent.mkdir(parents=True, exist_ok=True)


def _configure_connection(connection: sqlite3.Connection, _record: ConnectionPoolEntry) -> None:
    # The driver must not open transactions on its own: _begin does, with the chosen mode.
    connection.isolation_level = None
    connection.enable_load_extension(True)
    try:
        sqlite_vec.load(connection)
    finally:
        connection.enable_load_extension(False)
    cursor = connection.cursor()
    try:
        for pragma in CONNECTION_PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()
