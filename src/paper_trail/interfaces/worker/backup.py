"""The worker's nightly database backup, a step of its loop like the jobs."""

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol

import structlog

from paper_trail.ports.clock import Clock

log = structlog.get_logger(__name__)

RETRY_AFTER = timedelta(hours=1)


class Backup(Protocol):
    """What a backup reports (``bootstrap.backup_database``)."""

    @property
    def path(self) -> Path: ...

    @property
    def size_bytes(self) -> int: ...

    @property
    def removed(self) -> tuple[Path, ...]: ...


class NightlyBackup:
    """A worker step: once a day, from ``at`` local time on, ``backup`` the database unless that
    day's backup exists (``exists``), so a worker that was down at ``at`` backs up as soon as it
    runs again that day. A failure is logged and tried again an hour later.
    """

    def __init__(
        self,
        backup: Callable[[date], Backup],
        exists: Callable[[date], bool],
        clock: Clock,
        *,
        at: time,
    ) -> None:
        self._backup = backup
        self._exists = exists
        self._clock = clock
        self._at = at
        self._done: date | None = None
        self._retry_at: datetime | None = None

    def __call__(self) -> bool:
        """True when a backup was made, False when none was due (or it failed)."""
        now = self._clock.now().astimezone()
        today = now.date()
        if now.time() < self._at or self._done == today:
            return False
        if self._retry_at is not None and now < self._retry_at:
            return False
        if self._exists(today):
            self._done = today
            return False
        try:
            backup = self._backup(today)
        except Exception as error:  # a failed backup must not stop the worker
            self._retry_at = now + RETRY_AFTER
            log.error("backup.failed", error_type=type(error).__name__, error=str(error))
            return False
        self._done, self._retry_at = today, None
        log.info(
            "backup.written",
            path=str(backup.path),
            size_bytes=backup.size_bytes,
            removed=[path.name for path in backup.removed],
        )
        return True
