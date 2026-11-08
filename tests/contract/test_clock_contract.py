"""Clock port contract, run against the fake and the real adapter (S0-AC-24, PLAN 1.7)."""

from collections.abc import Callable

import pytest

from paper_trail.adapters.clock import SystemClock
from paper_trail.ports.clock import Clock
from tests.fakes.clock import FakeClock

CLOCKS: dict[str, Callable[[], Clock]] = {"fake": FakeClock, "system": SystemClock}


@pytest.fixture(params=sorted(CLOCKS))
def clock(request: pytest.FixtureRequest) -> Clock:
    return CLOCKS[request.param]()


def test_s0_ac_24_clock_returns_timezone_aware_utc(clock: Clock) -> None:
    """Clock contract: now() is timezone-aware UTC (PLAN 1.2)."""
    now = clock.now()

    offset = now.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0


def test_s0_ac_24_clock_never_goes_backwards(clock: Clock) -> None:
    """Clock contract: consecutive readings are non-decreasing."""
    first = clock.now()
    second = clock.now()

    assert second >= first
