"""The worker's nightly backup step (``interfaces.worker.backup.NightlyBackup``).

Pinned: nothing before ``at`` (local time); from ``at`` on, one backup a day, skipped when that
day's backup exists, made late when the worker was not running at ``at``; a failure is logged
(``backup.failed``) and tried again an hour later; a backup is logged (``backup.written``).
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from paper_trail.interfaces.worker.backup import NightlyBackup
from tests.fakes.clock import FakeClock

AT = time(3, 30)


@dataclass(frozen=True)
class _Backup:
    path: Path
    size_bytes: int = 4096
    removed: tuple[Path, ...] = ()


@dataclass
class Backups:
    """Fake backups: which days were backed up, which already exist, which fail."""

    existing: set[date] = field(default_factory=set)
    failing: set[date] = field(default_factory=set)
    made: list[date] = field(default_factory=list)

    def backup(self, day: date) -> _Backup:
        self.made.append(day)
        if day in self.failing:
            msg = "disk full"
            raise OSError(msg)
        self.existing.add(day)
        return _Backup(Path(f"/data/backups/paper_trail-{day:%Y%m%d}.db"), removed=(Path("x"),))


def _local(day: int, hour: int, minute: int) -> datetime:
    """That wall-clock time in the local time zone, in UTC (the clock's time)."""
    return datetime(2026, 11, day, hour, minute).astimezone().astimezone(UTC)


def _step(backups: Backups, clock: FakeClock) -> NightlyBackup:
    return NightlyBackup(backups.backup, backups.existing.__contains__, clock, at=AT)


@pytest.mark.parametrize(
    ("times", "made"),
    [
        ([(18, 3, 29)], []),
        ([(18, 3, 29), (18, 3, 30), (18, 3, 31), (18, 23, 59)], [18]),
        ([(18, 14, 0)], [18]),  # the worker was down at 03:30
        ([(18, 3, 30), (19, 1, 0), (19, 3, 30), (20, 9, 0)], [18, 19, 20]),
    ],
)
def test_one_backup_a_day_from_the_time_on(
    times: list[tuple[int, int, int]], made: list[int]
) -> None:
    backups, clock = Backups(), FakeClock()
    step = _step(backups, clock)

    results = []
    for day, hour, minute in times:
        clock.set(_local(day, hour, minute))
        results.append(step())

    assert backups.made == [date(2026, 11, day) for day in made]
    assert results.count(True) == len(made)


def test_a_day_already_backed_up_is_not_backed_up_again() -> None:
    backups, clock = Backups(existing={date(2026, 11, 18)}), FakeClock(_local(18, 4, 0))

    assert _step(backups, clock)() is False
    assert backups.made == []


def test_a_failed_backup_is_logged_and_tried_again_an_hour_later() -> None:
    backups = Backups(failing={date(2026, 11, 18)})
    clock = FakeClock(_local(18, 3, 30))
    step = _step(backups, clock)

    with capture_logs() as logs:
        first = step()
        backups.failing.clear()
        clock.set(_local(18, 4, 29))
        too_soon = step()
        clock.set(_local(18, 4, 30))
        retried = step()
        clock.set(_local(18, 5, 30))
        done = step()

    assert (first, too_soon, retried, done) == (False, False, True, False)
    assert backups.made == [date(2026, 11, 18), date(2026, 11, 18)]
    assert [(entry["event"], entry["log_level"]) for entry in logs] == [
        ("backup.failed", "error"),
        ("backup.written", "info"),
    ]
    assert logs[0]["error_type"] == "OSError"
    assert logs[1] == {
        "event": "backup.written",
        "log_level": "info",
        "path": "/data/backups/paper_trail-20261118.db",
        "size_bytes": 4096,
        "removed": ["x"],
    }
