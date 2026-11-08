"""HeartbeatFile freshness and atomic writes (S0-AC-24)."""

from datetime import timedelta
from pathlib import Path

from paper_trail.interfaces.worker.heartbeat import HeartbeatFile
from tests.fakes.clock import FakeClock


def test_s0_ac_24_missing_file_is_not_fresh(tmp_path: Path) -> None:
    """S0-AC-24: no heartbeat file means not fresh."""
    heartbeat = HeartbeatFile(tmp_path / "worker.heartbeat", FakeClock())

    assert heartbeat.is_fresh(timedelta(seconds=30)) is False


def test_s0_ac_24_fresh_until_max_age_then_stale(tmp_path: Path) -> None:
    """S0-AC-24: a beat is fresh while younger than max_age and stale afterwards."""
    clock = FakeClock()
    heartbeat = HeartbeatFile(tmp_path / "worker.heartbeat", clock)
    heartbeat.beat()

    clock.advance(29)
    assert heartbeat.is_fresh(timedelta(seconds=30)) is True

    clock.advance(2)
    assert heartbeat.is_fresh(timedelta(seconds=30)) is False


def test_s0_ac_24_beat_refreshes_the_timestamp(tmp_path: Path) -> None:
    """S0-AC-24: a later beat makes a stale heartbeat fresh again."""
    clock = FakeClock()
    heartbeat = HeartbeatFile(tmp_path / "worker.heartbeat", clock)
    heartbeat.beat()
    clock.advance(60)
    heartbeat.beat()

    assert heartbeat.is_fresh(timedelta(seconds=30)) is True


def test_s0_ac_24_file_holds_an_iso_timestamp_and_no_temp_files_remain(tmp_path: Path) -> None:
    """S0-AC-24: beat() atomically writes an ISO-8601 timestamp (tmp file + os.replace)."""
    clock = FakeClock()
    # A directory of its own: tmp_path also holds the "cwd" dir of the autouse env fixture.
    path = tmp_path / "heartbeat" / "worker.heartbeat"
    HeartbeatFile(path, clock).beat()

    assert path.read_text(encoding="utf-8").strip() == clock.now().isoformat()
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_s0_ac_24_unreadable_content_is_not_fresh(tmp_path: Path) -> None:
    """S0-AC-24: a corrupt heartbeat file is treated as stale, never as an error."""
    path = tmp_path / "worker.heartbeat"
    path.write_text("not a timestamp", encoding="utf-8")

    assert HeartbeatFile(path, FakeClock()).is_fresh(timedelta(seconds=30)) is False


def test_s0_ac_24_beat_creates_the_parent_directory(tmp_path: Path) -> None:
    """S0-AC-24: the first beat works on an empty data volume."""
    path = tmp_path / "nested" / "worker.heartbeat"

    HeartbeatFile(path, FakeClock()).beat()

    assert path.exists()
