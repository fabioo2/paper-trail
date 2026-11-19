"""``local_today``: the one "today" of every date check, in the app's time zone (``TZ``)."""

import time
from datetime import UTC, date, datetime

import pytest

from paper_trail.ports.clock import local_today
from tests.fakes.clock import FakeClock


@pytest.mark.parametrize(
    ("now", "today"),
    [
        (datetime(2026, 11, 7, 3, 30, tzinfo=UTC), date(2026, 11, 6)),
        (datetime(2026, 11, 7, 5, 0, tzinfo=UTC), date(2026, 11, 7)),
    ],
    ids=["utc-already-tomorrow", "toronto-midnight-passed"],
)
def test_today_is_the_app_s_local_date_not_utc_s(
    monkeypatch: pytest.MonkeyPatch, now: datetime, today: date
) -> None:
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()
    try:
        assert local_today(FakeClock(now)) == today
    finally:
        monkeypatch.undo()
        time.tzset()
