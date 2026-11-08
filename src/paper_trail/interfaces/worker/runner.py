"""The worker loop: run step handlers, heartbeat, wait when idle, stop gracefully."""

import os
import signal
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from types import FrameType
from typing import Protocol

import structlog

from paper_trail.interfaces.worker.heartbeat import HeartbeatFile
from paper_trail.ports.clock import Clock

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
FORCED_EXIT_CODE = 1

log = structlog.get_logger(__name__)


class StepHandler(Protocol):
    def __call__(self) -> bool:
        """Do one unit of work. True if work was done (loop again at once), False if idle."""
        ...


class WorkerRunner:
    """Calls every handler in turn until stopped.

    When every handler was idle the loop waits ``poll_interval_s`` before the next round,
    waking early to refresh the heartbeat (at least every ``heartbeat_interval_s``) and as soon
    as a stop is requested. SIGTERM or SIGINT (or ``request_stop``) lets the in-flight handler
    call finish and then ends the loop; if that takes longer than ``shutdown_grace_s`` after a
    signal, a watchdog exits the process with a failure code.

    ``wait`` blocks for at most the given number of seconds and may return early; it defaults
    to waiting on the runner's internal stop event, which ``request_stop`` sets.
    """

    def __init__(  # noqa: PLR0913 - keyword-only signature fixed by PLAN 2.8
        self,
        *,
        handlers: Sequence[StepHandler],
        clock: Clock,
        heartbeat: HeartbeatFile,
        poll_interval_s: float,
        heartbeat_interval_s: float,
        shutdown_grace_s: float,
        wait: Callable[[float], object] | None = None,
    ) -> None:
        self._handlers = tuple(handlers)
        self._clock = clock
        self._heartbeat = heartbeat
        self._poll_interval = timedelta(seconds=poll_interval_s)
        self._heartbeat_interval = timedelta(seconds=heartbeat_interval_s)
        self._shutdown_grace_s = shutdown_grace_s
        # The flag is what the loop checks; the event only wakes an idle wait. They are separate
        # because a signal handler may set the flag but must not take the event's lock.
        self._stop_requested = False
        self._wake = threading.Event()
        self._wait = wait if wait is not None else self._wake.wait
        self._last_beat: datetime | None = None
        self._watchdog: threading.Timer | None = None

    def run(self) -> int:
        """Run until stopped. Installs SIGTERM/SIGINT handlers for the duration; returns 0."""
        previous = {signum: signal.signal(signum, self._on_signal) for signum in STOP_SIGNALS}
        try:
            log.info("worker.started", handlers=len(self._handlers))
            self._loop()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            if self._watchdog is not None:
                self._watchdog.cancel()
        log.info("worker.stopped")
        return 0

    def request_stop(self) -> None:
        """Finish the current handler call, then leave the loop. Safe from any thread."""
        self._mark_stopping()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop_requested:
            self._beat_if_due()
            if not self._run_handlers():
                self._idle()

    def _run_handlers(self) -> bool:
        busy = False
        for handler in self._handlers:
            if self._stop_requested:
                break
            busy = handler() or busy
        return busy

    def _idle(self) -> None:
        """Wait out one poll interval, heartbeating on time and returning early on stop."""
        deadline = self._clock.now() + self._poll_interval
        while not self._stop_requested:
            next_beat = self._beat_if_due()
            now = self._clock.now()
            if now >= deadline:
                return
            self._wait((min(deadline, next_beat) - now).total_seconds())

    def _beat_if_due(self) -> datetime:
        """Refresh the heartbeat if an interval has passed; return when the next one is due."""
        now = self._clock.now()
        if self._last_beat is None or now - self._last_beat >= self._heartbeat_interval:
            self._heartbeat.beat()
            self._last_beat = now
        return self._last_beat + self._heartbeat_interval

    def _mark_stopping(self) -> None:
        if not self._stop_requested:
            self._stop_requested = True
            log.info("worker.stopping")

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        log.info("worker.signal_received", signal=signal.Signals(signum).name)
        if self._watchdog is None:
            self._watchdog = threading.Timer(self._shutdown_grace_s, self._force_exit)
            self._watchdog.daemon = True
            self._watchdog.start()
        self._mark_stopping()
        # Event.set() takes a lock the interrupted main thread may be holding inside
        # Event.wait(), so the idle wait is woken from another thread.
        threading.Thread(target=self._wake.set, name="worker-wake", daemon=True).start()

    def _force_exit(self) -> None:
        log.error("worker.shutdown_timeout", grace_s=self._shutdown_grace_s)
        os._exit(FORCED_EXIT_CODE)
