"""System clock adapter."""

from datetime import UTC, datetime


class SystemClock:
    """Implements ``paper_trail.ports.clock.Clock`` with the operating system's clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)
