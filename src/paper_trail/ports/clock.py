"""Clock port: the only source of the current time."""

from datetime import date, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


def local_today(clock: Clock) -> date:
    """Today in the app's time zone (the process's ``TZ``, America/Toronto in the containers):
    the one "today" every date check uses."""
    return clock.now().astimezone().date()
