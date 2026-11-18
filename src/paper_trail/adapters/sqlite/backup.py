"""Online backups of the SQLite database, one file per day (SQLite's backup API).

The copy is taken through a read transaction, so the web app and the worker keep writing
meanwhile (WAL mode); it is written beside the backups under a temporary name, switched to a
rollback journal (one self-contained file to restore), checked with ``integrity_check`` and only
then renamed into place, so a backup file is always a complete, checked copy. The oldest are
removed beyond ``keep``. Each backup copies into a temporary file of its own, and putting it in
place and removing the oldest are serialized across processes (a lock file in the folder), so
`make backup` running beside the worker's nightly backup cannot disturb it.
"""

import fcntl
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

BACKUP_NAME = re.compile(r"paper_trail-\d{8}\.db")
LOCK_FILENAME = ".backup.lock"


class BackupError(Exception):
    """The copy failed its integrity check; nothing was kept."""


@dataclass(frozen=True, slots=True)
class DatabaseBackup:
    path: Path
    size_bytes: int
    removed: tuple[Path, ...]  # older backups deleted beyond ``keep``


def backup_path(directory: Path, day: date) -> Path:
    return directory / f"paper_trail-{day:%Y%m%d}.db"


def write_backup(database: Path, directory: Path, day: date, *, keep: int) -> DatabaseBackup:
    """Copy ``database`` (which must exist) to ``backup_path(directory, day)``, replacing a
    backup of that day, then keep only the newest ``keep`` backups. BackupError when the copy
    is not intact; sqlite3.Error when the database cannot be read."""
    directory.mkdir(parents=True, exist_ok=True)
    target = backup_path(directory, day)
    with tempfile.NamedTemporaryFile(
        dir=directory, prefix=f".{target.name}.", suffix=".partial", delete=False
    ) as created:
        partial = Path(created.name)
    try:
        with (
            closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(partial)) as copy,
        ):
            source.backup(copy)
            copy.execute("PRAGMA journal_mode=DELETE")
            problems = _integrity_problems(copy)
        if problems != ["ok"]:
            msg = f"the copy of {database} is not intact: {'; '.join(problems[:5])}"
            raise BackupError(msg)
        with _locked(directory):
            partial.replace(target)
            size = target.stat().st_size
            removed = _prune(directory, keep)
    finally:
        partial.unlink(missing_ok=True)
    return DatabaseBackup(target, size, removed)


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """The folder's backups to this process alone for the block."""
    with (directory / LOCK_FILENAME).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _integrity_problems(database: sqlite3.Connection) -> list[str]:
    """``["ok"]`` for an intact database, else what is wrong (a page too damaged to check is
    reported by an error instead of a row)."""
    try:
        return [row[0] for row in database.execute("PRAGMA integrity_check")]
    except sqlite3.DatabaseError as error:
        return [str(error)]


def _prune(directory: Path, keep: int) -> tuple[Path, ...]:
    """Delete all but the newest ``keep`` backups (by the day in their name)."""
    backups = sorted(
        (path for path in directory.iterdir() if BACKUP_NAME.fullmatch(path.name)), reverse=True
    )
    for old in backups[keep:]:
        old.unlink()
    return tuple(backups[keep:])
