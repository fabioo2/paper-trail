"""WorkerRunner loop, heartbeat cadence and graceful shutdown (S0-AC-24, S0-AC-25)."""

import os
import signal
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest

from paper_trail.adapters.clock import SystemClock
from paper_trail.config import Settings
from paper_trail.interfaces.worker.heartbeat import HeartbeatFile
from paper_trail.interfaces.worker.runner import StepHandler, WorkerRunner
from paper_trail.observability import configure_logging
from tests.fakes.clock import FakeClock
from tests.support import JsonLogs, MakeSettings

POLL_S = 1.0
HEARTBEAT_S = 3.0
GRACE_S = 8.0


@dataclass
class ScriptedHandler:
    """A StepHandler returning scripted results; an optional hook runs during each call."""

    results: list[bool]
    during_call: Callable[[int], None] | None = None
    calls: int = 0
    completed: int = 0

    def __call__(self) -> bool:
        self.calls += 1
        if self.during_call is not None:
            self.during_call(self.calls)
        result = self.results[min(self.calls, len(self.results)) - 1]
        self.completed += 1
        return result


@dataclass
class WaitSpy:
    """Injected wait: records durations, advances the fake clock, optionally stops the runner."""

    clock: FakeClock
    stop_after: int
    runner: WorkerRunner | None = None
    on_wait: Callable[[], None] | None = None
    durations: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        if self.on_wait is not None:
            self.on_wait()
        self.durations.append(seconds)
        self.clock.advance(seconds)
        if len(self.durations) >= self.stop_after and self.runner is not None:
            self.runner.request_stop()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def heartbeat(tmp_path: Path, clock: FakeClock) -> HeartbeatFile:
    return HeartbeatFile(tmp_path / "worker.heartbeat", clock)


@pytest.fixture
def logged(make_settings: MakeSettings) -> Settings:
    settings = make_settings(log_level="DEBUG", log_format="json")
    configure_logging(settings)
    return settings


@pytest.fixture(autouse=True)
def _signal_handlers_restored() -> Iterator[None]:
    """A runner that leaks a SIGTERM handler would make later tests unkillable."""
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    yield
    after = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    signal.signal(signal.SIGTERM, before[0])
    signal.signal(signal.SIGINT, before[1])
    assert after == before, "WorkerRunner.run() must restore the previous signal handlers"


def _runner(
    *,
    handlers: Sequence[StepHandler],
    clock: FakeClock,
    heartbeat: HeartbeatFile,
    wait: Callable[[float], None],
    poll_interval_s: float = POLL_S,
) -> WorkerRunner:
    return WorkerRunner(
        handlers=handlers,
        clock=clock,
        heartbeat=heartbeat,
        poll_interval_s=poll_interval_s,
        heartbeat_interval_s=HEARTBEAT_S,
        shutdown_grace_s=GRACE_S,
        wait=wait,
    )


def test_s0_ac_24_idle_runner_beats_at_least_every_interval(
    clock: FakeClock, heartbeat: HeartbeatFile
) -> None:
    """S0-AC-24: with no handlers the runner heartbeats at least every heartbeat_interval_s."""
    # Half a poll of slack: a missed beat would be a whole poll (1 s) older than the interval.
    max_age = timedelta(seconds=HEARTBEAT_S + POLL_S / 2)
    observed: list[bool] = []
    wait = WaitSpy(
        clock=clock, stop_after=20, on_wait=lambda: observed.append(heartbeat.is_fresh(max_age))
    )
    runner = _runner(handlers=(), clock=clock, heartbeat=heartbeat, wait=wait)
    wait.runner = runner

    assert runner.run() == 0

    assert len(observed) == 20
    assert all(observed), f"heartbeat went stale during idle loop: {observed}"


def test_s0_ac_24_first_beat_happens_before_the_first_wait(
    clock: FakeClock, heartbeat: HeartbeatFile
) -> None:
    """S0-AC-24: a fresh heartbeat exists as soon as the loop starts."""
    seen: list[bool] = []
    wait = WaitSpy(
        clock=clock,
        stop_after=1,
        on_wait=lambda: seen.append(heartbeat.is_fresh(timedelta(seconds=0.001))),
    )
    runner = _runner(handlers=(), clock=clock, heartbeat=heartbeat, wait=wait)
    wait.runner = runner

    runner.run()

    assert seen == [True]


def test_s0_ac_25_idle_handler_waits_poll_interval(
    clock: FakeClock, heartbeat: HeartbeatFile
) -> None:
    """S0-AC-25: a handler returning False causes a wait of poll_interval_s."""
    handler = ScriptedHandler(results=[False])
    wait = WaitSpy(clock=clock, stop_after=3)
    runner = _runner(handlers=[handler], clock=clock, heartbeat=heartbeat, wait=wait)
    wait.runner = runner

    assert runner.run() == 0

    assert wait.durations == [POLL_S, POLL_S, POLL_S]
    assert handler.calls == 3


def test_s0_ac_25_busy_handler_continues_without_waiting(
    clock: FakeClock, heartbeat: HeartbeatFile
) -> None:
    """S0-AC-25: a handler returning True causes the next iteration immediately."""
    handler = ScriptedHandler(results=[True, True, True, False])
    wait = WaitSpy(clock=clock, stop_after=1)
    runner = _runner(handlers=[handler], clock=clock, heartbeat=heartbeat, wait=wait)
    wait.runner = runner

    runner.run()

    assert handler.calls == 4, "three busy iterations, then the idle one that waits"
    assert wait.durations == [POLL_S]


def test_s0_ac_25_any_busy_handler_skips_the_wait(
    clock: FakeClock, heartbeat: HeartbeatFile
) -> None:
    """S0-AC-25: with several handlers, the loop waits only when all of them were idle."""
    idle = ScriptedHandler(results=[False])
    busy_once = ScriptedHandler(results=[True, False])
    wait = WaitSpy(clock=clock, stop_after=1)
    runner = _runner(handlers=[idle, busy_once], clock=clock, heartbeat=heartbeat, wait=wait)
    wait.runner = runner

    runner.run()

    assert busy_once.calls == 2
    assert wait.durations == [POLL_S]


def test_s0_ac_25_stop_requested_mid_step_finishes_the_current_call(
    clock: FakeClock, heartbeat: HeartbeatFile, logged: Settings, json_logs: JsonLogs
) -> None:
    """S0-AC-25: a stop request lets the in-flight handler finish, then the loop exits 0."""
    runner_ref: list[WorkerRunner] = []
    handler = ScriptedHandler(
        results=[True], during_call=lambda call: runner_ref[0].request_stop() if call == 2 else None
    )
    wait = WaitSpy(clock=clock, stop_after=10_000)
    runner = _runner(handlers=[handler], clock=clock, heartbeat=heartbeat, wait=wait)
    runner_ref.append(runner)

    assert runner.run() == 0

    assert handler.calls == 2
    assert handler.completed == 2, "the in-flight call must run to completion"
    events = [r["event"] for r in json_logs()]
    assert events.index("worker.stopping") < events.index("worker.stopped")


def test_s0_ac_25_sigterm_during_a_step_stops_gracefully(
    clock: FakeClock, heartbeat: HeartbeatFile, logged: Settings, json_logs: JsonLogs
) -> None:
    """S0-AC-25: SIGTERM is handled by run(): log worker.stopping, finish the step, exit 0."""

    def send_sigterm(call: int) -> None:
        if call == 1:
            os.kill(os.getpid(), signal.SIGTERM)

    handler = ScriptedHandler(results=[True], during_call=send_sigterm)
    wait = WaitSpy(clock=clock, stop_after=10_000)
    runner = _runner(handlers=[handler], clock=clock, heartbeat=heartbeat, wait=wait)

    assert runner.run() == 0

    assert handler.calls == 1
    assert handler.completed == 1
    events = [r["event"] for r in json_logs()]
    assert "worker.stopping" in events
    assert events[-1] == "worker.stopped"


def test_s0_ac_25_sigint_is_handled_like_sigterm(
    clock: FakeClock, heartbeat: HeartbeatFile
) -> None:
    """S0-AC-25: Ctrl-C (SIGINT) also stops the loop gracefully with exit code 0."""
    handler = ScriptedHandler(
        results=[True], during_call=lambda _call: os.kill(os.getpid(), signal.SIGINT)
    )
    runner = _runner(
        handlers=[handler], clock=clock, heartbeat=heartbeat, wait=WaitSpy(clock, 10_000)
    )

    assert runner.run() == 0
    assert handler.calls == 1


def test_s0_ac_25_stop_while_idle_does_not_call_handlers_again(
    clock: FakeClock, heartbeat: HeartbeatFile
) -> None:
    """S0-AC-25: a stop requested during the idle wait ends the loop before the next step."""
    handler = ScriptedHandler(results=[False])
    wait = WaitSpy(clock=clock, stop_after=1)
    runner = _runner(handlers=[handler], clock=clock, heartbeat=heartbeat, wait=wait)
    wait.runner = runner

    runner.run()

    assert handler.calls == 1


def test_s0_ac_24_poll_longer_than_heartbeat_still_beats_on_time(
    clock: FakeClock, heartbeat: HeartbeatFile
) -> None:
    """S0-AC-24: an idle wait never outlasts the heartbeat interval, whatever the poll is."""
    long_poll_s = 10 * HEARTBEAT_S
    observed: list[bool] = []
    handler = ScriptedHandler(results=[False])
    wait = WaitSpy(
        clock=clock,
        stop_after=25,
        on_wait=lambda: observed.append(heartbeat.is_fresh(timedelta(seconds=HEARTBEAT_S))),
    )
    runner = _runner(
        handlers=[handler], clock=clock, heartbeat=heartbeat, wait=wait, poll_interval_s=long_poll_s
    )
    wait.runner = runner

    assert runner.run() == 0

    assert all(seconds <= HEARTBEAT_S for seconds in wait.durations), wait.durations
    assert all(observed), f"heartbeat went stale during a long poll: {observed}"
    assert handler.calls == 3, "the handler still runs once per poll interval (0 s, 30 s, 60 s)"


def _real_runner(heartbeat_path: Path, *, poll_interval_s: float, grace_s: float) -> WorkerRunner:
    clock = SystemClock()
    return WorkerRunner(
        handlers=(),
        clock=clock,
        heartbeat=HeartbeatFile(heartbeat_path, clock),
        poll_interval_s=poll_interval_s,
        heartbeat_interval_s=poll_interval_s,
        shutdown_grace_s=grace_s,
    )


def test_s0_ac_25_sigterm_while_idle_interrupts_a_long_poll(tmp_path: Path) -> None:
    """S0-AC-25: with the real wait, SIGTERM during a poll longer than the shutdown grace ends
    the loop at once (exit 0) instead of waiting out the poll and tripping the watchdog."""
    # The grace is longer than the test may take, so a regression fails the timing assertion
    # instead of the watchdog killing the test process.
    runner = _real_runner(tmp_path / "worker.heartbeat", poll_interval_s=60.0, grace_s=120.0)
    sender = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGTERM))
    started = time.monotonic()
    sender.start()
    try:
        exit_code = runner.run()
    finally:
        sender.cancel()

    assert exit_code == 0
    assert time.monotonic() - started < 5


def test_s0_ac_25_sigterm_with_the_normal_short_poll_stops_promptly(tmp_path: Path) -> None:
    """S0-AC-25 with the poll the worker really runs with (a fraction of a second, as in
    ``WORKER__POLL_INTERVAL_S``): SIGTERM between two polls ends the loop with exit 0 within
    a poll or so, and the heartbeat was written (test-suite cleanup review note: the process
    test only covers a poll longer than the grace)."""
    heartbeat = tmp_path / "worker.heartbeat"
    runner = _real_runner(heartbeat, poll_interval_s=0.1, grace_s=120.0)
    sender = threading.Timer(0.35, os.kill, (os.getpid(), signal.SIGTERM))
    started = time.monotonic()
    sender.start()
    try:
        exit_code = runner.run()
    finally:
        sender.cancel()

    assert exit_code == 0
    assert 0.3 < time.monotonic() - started < 5
    assert heartbeat.is_file()


def test_s0_ac_25_request_stop_from_another_thread_wakes_the_idle_wait(tmp_path: Path) -> None:
    """S0-AC-25: request_stop() from any thread interrupts the idle wait promptly."""
    runner = _real_runner(tmp_path / "worker.heartbeat", poll_interval_s=60.0, grace_s=120.0)
    stopper = threading.Timer(0.2, runner.request_stop)
    started = time.monotonic()
    stopper.start()
    try:
        exit_code = runner.run()
    finally:
        stopper.cancel()

    assert exit_code == 0
    assert time.monotonic() - started < 5
