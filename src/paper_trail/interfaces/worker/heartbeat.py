"""A heartbeat file the worker refreshes and its container healthcheck reads."""

from datetime import datetime, timedelta
from pathlib import Path

from paper_trail.ports.clock import Clock

HEARTBEAT_FILENAME = "worker.heartbeat"


class HeartbeatFile:
    def __init__(self, path: Path, clock: Clock) -> None:
        self._path = path
        self._clock = clock

    def beat(self) -> None:
        """Record "alive now" atomically (write a temp file, then rename over the old one)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        temporary.write_text(self._clock.now().isoformat(), encoding="utf-8")
        temporary.replace(self._path)

    def is_fresh(self, max_age: timedelta) -> bool:
        """True when the last beat is younger than ``max_age``; missing or corrupt means stale."""
        try:
            last_beat = datetime.fromisoformat(self._path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return False
        if last_beat.tzinfo is None:
            return False
        return self._clock.now() - last_beat < max_age
