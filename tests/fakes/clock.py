"""In-memory Clock for deterministic tests (implements ``paper_trail.ports.clock.Clock``)."""

from datetime import UTC, datetime, timedelta

DEFAULT_START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class FakeClock:
    """A settable, advanceable clock. Always returns timezone-aware UTC datetimes."""

    def __init__(self, start: datetime = DEFAULT_START) -> None:
        self._now = self._require_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = self._require_utc(value)

    def advance(self, delta: timedelta | float) -> None:
        step = delta if isinstance(delta, timedelta) else timedelta(seconds=delta)
        if step < timedelta(0):
            msg = "FakeClock cannot move backwards"
            raise ValueError(msg)
        self._now += step

    @staticmethod
    def _require_utc(value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            msg = "FakeClock requires a timezone-aware UTC datetime"
            raise ValueError(msg)
        return value
