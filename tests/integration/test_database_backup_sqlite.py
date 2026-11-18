"""Online backups of the SQLite database (``adapters.sqlite.backup.write_backup``) on real files.

Pinned: the backup is a self-contained copy (rollback journal, no ``-wal``) of what was
committed, rows still in the source's WAL included, taken while another connection holds the
write lock with uncommitted rows; it is named by its day, replaces that day's, and only the
newest ``keep`` backups are kept (other files in the folder are left alone, another backup's
temporary file included, and a backup waits for another holding the folder's lock before
putting its copy in place); a relative database path works; a copy that fails
``integrity_check`` raises BackupError and leaves nothing (no file, no partial, older backups
untouched); a missing database raises without creating one.
"""

import fcntl
import sqlite3
import threading
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest

from paper_trail.adapters.sqlite.backup import BackupError, DatabaseBackup, write_backup
from tests.support import CopyMigrated

DAY = date(2026, 11, 18)


def _query(database: Path, sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(sql).fetchall()


@pytest.fixture
def database(tmp_path: Path, copy_migrated: CopyMigrated) -> Path:
    path = copy_migrated(tmp_path / "data" / "paper_trail.db")
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE backup_probe (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO backup_probe VALUES (1), (2)")
    return path


@pytest.fixture
def backups(tmp_path: Path) -> Path:
    return tmp_path / "data" / "backups"


def test_the_backup_is_a_whole_copy_of_what_was_committed_while_a_writer_holds_the_lock(
    database: Path, backups: Path
) -> None:
    with (
        closing(sqlite3.connect(database, isolation_level=None)) as committed,
        closing(sqlite3.connect(database, isolation_level=None)) as writer,
    ):
        committed.execute("PRAGMA wal_autocheckpoint=0")
        committed.execute("INSERT INTO backup_probe VALUES (4)")
        assert database.with_name("paper_trail.db-wal").stat().st_size > 0
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO backup_probe VALUES (3)")

        backup = write_backup(database, backups, DAY, keep=14)

        writer.execute("ROLLBACK")

    copy = backups / "paper_trail-20261118.db"
    assert backup == DatabaseBackup(copy, copy.stat().st_size, ())
    assert sorted(path.name for path in backups.iterdir()) == [
        ".backup.lock",
        "paper_trail-20261118.db",
    ]
    assert _query(copy, "SELECT id FROM backup_probe ORDER BY id") == [(1,), (2,), (4,)]
    assert _query(copy, "PRAGMA journal_mode") == [("delete",)]
    assert _query(copy, "PRAGMA integrity_check") == [("ok",)]
    assert _query(copy, "SELECT version_num FROM alembic_version") == _query(
        database, "SELECT version_num FROM alembic_version"
    )


def test_a_relative_database_path_is_backed_up(
    database: Path, backups: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(database.parent)

    backup = write_backup(Path(database.name), backups, DAY, keep=14)

    assert _query(backup.path, "SELECT count(*) FROM backup_probe") == [(2,)]


def test_a_backup_leaves_the_temporary_file_of_another_one_alone(
    database: Path, backups: Path
) -> None:
    backups.mkdir(parents=True)
    other = backups / ".paper_trail-20261118.db.partial"
    other.write_bytes(b"another backup's copy in progress")

    write_backup(database, backups, DAY, keep=14)

    assert other.read_bytes() == b"another backup's copy in progress"
    assert sorted(path.name for path in backups.iterdir() if not path.name.startswith(".")) == [
        "paper_trail-20261118.db"
    ]


def test_a_backup_waits_for_another_putting_its_copy_in_place(
    database: Path, backups: Path
) -> None:
    backups.mkdir(parents=True)
    done = threading.Event()

    def back_up() -> None:
        write_backup(database, backups, DAY, keep=14)
        done.set()

    with (backups / ".backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        thread = threading.Thread(target=back_up)
        thread.start()
        waited = done.wait(1.0)
        published_while_locked = (backups / "paper_trail-20261118.db").exists()
        fcntl.flock(lock, fcntl.LOCK_UN)
    thread.join(timeout=30)

    assert (waited, published_while_locked, done.is_set()) == (False, False, True)


def test_a_second_backup_on_the_same_day_replaces_it(database: Path, backups: Path) -> None:
    write_backup(database, backups, DAY, keep=14)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("INSERT INTO backup_probe VALUES (3)")

    write_backup(database, backups, DAY, keep=14)

    assert [path.name for path in backups.iterdir() if not path.name.startswith(".")] == [
        "paper_trail-20261118.db"
    ]
    assert _query(backups / "paper_trail-20261118.db", "SELECT count(*) FROM backup_probe") == [
        (3,)
    ]


def test_only_the_newest_backups_are_kept(database: Path, backups: Path) -> None:
    backups.mkdir(parents=True)
    for days_ago in range(1, 6):
        (backups / f"paper_trail-{DAY - timedelta(days=days_ago):%Y%m%d}.db").write_bytes(b"old")
    (backups / "notes.txt").write_text("mine")
    (backups / "paper_trail-manual.db").write_bytes(b"mine")

    backup = write_backup(database, backups, DAY, keep=3)

    assert [path.name for path in backup.removed] == [
        "paper_trail-20261115.db",
        "paper_trail-20261114.db",
        "paper_trail-20261113.db",
    ]
    assert sorted(path.name for path in backups.iterdir()) == [
        ".backup.lock",
        "notes.txt",
        "paper_trail-20261116.db",
        "paper_trail-20261117.db",
        "paper_trail-20261118.db",
        "paper_trail-manual.db",
    ]


def test_a_copy_that_is_not_intact_is_not_kept(database: Path, backups: Path) -> None:
    write_backup(database, backups, DAY - timedelta(days=1), keep=1)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        (root_page,) = connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 'backup_probe'"
        ).fetchone()
        (page_size,) = connection.execute("PRAGMA page_size").fetchone()
    with database.open("r+b") as raw:
        raw.seek((root_page - 1) * page_size)
        raw.write(b"\x0d" + b"\xff" * 64)

    with pytest.raises(BackupError, match="is not intact"):
        write_backup(database, backups, DAY, keep=1)

    assert sorted(path.name for path in backups.iterdir()) == [
        ".backup.lock",
        "paper_trail-20261117.db",
    ]


def test_a_missing_database_is_not_created(tmp_path: Path, backups: Path) -> None:
    missing = tmp_path / "data" / "paper_trail.db"

    with pytest.raises(sqlite3.OperationalError):
        write_backup(missing, backups, DAY, keep=14)

    assert not missing.exists()
    assert list(backups.iterdir()) == []
