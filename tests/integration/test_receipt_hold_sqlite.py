"""The CLI running a stored receipt itself (``process --resume``, ``process --reprocess``,
``review resolve``) against the worker and the web app, on a real SQLite file with the graph
wired from fakes (``tests/agent_harness``).

Pinned: the CLI holds the receipt with a RUNNING job of its own (``ReceiptHold``), taken in one
transaction only while the receipt has no job waiting or running, so: each entry point is
refused (ConflictError, nothing changed, no read) while the worker holds the receipt; while the
CLI runs it the worker claims nothing and no second hold, Try again or review answer gets in;
racing a Try again or a second CLI, exactly one wins. The hold is renewed while it runs,
completed when the run ends and failed (re-raised) when it raises; a CLI that died leaves it to
lapse, and the worker then fails it and the receipt as stopped unexpectedly.
"""

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import Engine

from paper_trail.application.holds import JOB_LEASE, ReceiptHold
from paper_trail.application.jobs import STOPPED_UNEXPECTEDLY, ProcessReceiptJobs
from paper_trail.application.process_receipt import ProcessingStoppedError, RetryReceipt
from paper_trail.application.receipts import AnswerReview
from paper_trail.domain.enums import (
    AgentRole,
    FailureReason,
    JobKind,
    JobStatus,
    ProcessingStage,
    ReceiptStatus,
    ReviewStatus,
)
from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import Job, JsonValue
from paper_trail.ports.reader import ReaderUnavailableError
from paper_trail.ports.tools import ToolDescription, ToolGateway
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.agent_harness import CLI_HOLDER, Harness, agent_settings, build_harness
from tests.extractions import answer, inconsistent_answer
from tests.factories import receipt, receipt_draft
from tests.fakes.clock import FakeClock
from tests.fakes.reader import ScriptedReader
from tests.storage import in_unit, sqlite_uow_factory
from tests.support import require_id

WORKER = "worker-1"
ROUNDS = 20


class DuringRun:
    """Tools that call ``check`` once, at the run's first tool call: what the worker and the
    web app see while the CLI runs the receipt."""

    def __init__(self) -> None:
        self.check: Callable[[], None] = lambda: None
        self.inner: ToolGateway | None = None
        self.checked = False

    def wrap(self, inner: ToolGateway) -> ToolGateway:
        self.inner = inner
        return self

    def call(
        self, role: AgentRole, name: str, payload: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        if not self.checked:
            self.checked = True
            self.check()
        assert self.inner is not None
        return self.inner.call(role, name, payload)

    def describe(self, role: AgentRole) -> tuple[ToolDescription, ...]:
        assert self.inner is not None
        return self.inner.describe(role)


@dataclass
class Scene:
    """A stored receipt one CLI entry point runs (``run``), with Claude's answer scripted."""

    harness: Harness
    during: DuringRun
    receipt_id: int
    run: Callable[[], object]

    @property
    def reader(self) -> ScriptedReader:
        assert isinstance(self.harness.reader, ScriptedReader)
        return self.harness.reader

    def jobs(self) -> list[Job]:
        return _jobs(self.harness.uow_factory)

    def worker(self) -> ProcessReceiptJobs:
        harness = self.harness
        return ProcessReceiptJobs(
            harness.uow_factory,
            harness.process,
            harness.tracer,
            worker_id=WORKER,
            lease=JOB_LEASE,
            resolve_review=harness.resolve,
        )


def _resume() -> Scene:
    """``process --resume`` of a receipt whose read Claude stopped (FAILED, checkpoint kept)."""
    during = DuringRun()
    harness = build_harness(ScriptedReader(ReaderUnavailableError("down")), wrap_tools=during.wrap)
    with pytest.raises(ProcessingStoppedError) as stopped:
        harness.process_image()
    receipt_id = stopped.value.receipt_id
    scene = Scene(harness, during, receipt_id, lambda: harness.resume(receipt_id))
    scene.reader.script(answer())
    return scene


def _reprocess() -> Scene:
    """``process --reprocess`` of a completed receipt."""
    during = DuringRun()
    harness = build_harness(ScriptedReader(answer(), answer()), wrap_tools=during.wrap)
    receipt_id = harness.process_image().receipt_id
    during.checked = False
    return Scene(harness, during, receipt_id, lambda: harness.process_image(reprocess=True))


def _resolve() -> Scene:
    """``review resolve --file`` of a receipt parked for review."""
    during = DuringRun()
    harness = build_harness(
        ScriptedReader(inconsistent_answer()),
        settings=agent_settings(max_attempts=1),
        wrap_tools=during.wrap,
    )
    graph = harness.process_image().graph
    assert graph is not None
    review_id = require_id(graph.review_id)
    during.checked = False
    draft = receipt_draft(store_name="Costco", ocr_text=None, image_path=None)
    return Scene(harness, during, graph.receipt_id, lambda: harness.resolve(review_id, draft))


SCENES = {"resume": _resume, "reprocess": _reprocess, "resolve": _resolve}


def _state(scene: Scene) -> tuple[ReceiptStatus, ProcessingStage, tuple[ReviewStatus, ...]]:
    harness = scene.harness
    found = harness.receipt(scene.receipt_id)
    with harness.uow_factory(read_only=True) as uow:
        reviews = tuple(
            review.status
            for review_id in range(1, 4)
            if (review := uow.reviews.get(review_id)) is not None
        )
    return found.status, found.stage, reviews


@pytest.mark.parametrize("entry", SCENES)
def test_the_cli_is_refused_while_the_worker_holds_the_receipt(entry: str) -> None:
    scene = SCENES[entry]()
    uow_factory = scene.harness.uow_factory
    in_unit(
        uow_factory,
        lambda uow: uow.jobs.enqueue(JobKind.PROCESS_RECEIPT, scene.receipt_id, max_attempts=2),
    )
    claimed = in_unit(uow_factory, lambda uow: uow.jobs.claim(WORKER, JOB_LEASE))
    assert claimed is not None
    before = (_state(scene), scene.jobs(), scene.reader.calls)

    with pytest.raises(ConflictError, match=f"receipt {scene.receipt_id} is already being"):
        scene.run()

    assert (_state(scene), scene.jobs(), scene.reader.calls) == before
    assert not scene.during.checked


@pytest.mark.parametrize("entry", SCENES)
def test_while_the_cli_runs_a_receipt_nobody_else_can_take_it(entry: str) -> None:
    scene = SCENES[entry]()
    uow_factory = scene.harness.uow_factory
    seen: list[object] = []

    def check() -> None:
        seen.append(
            [
                (job.status, job.locked_by)
                for job in scene.jobs()
                if job.status in {JobStatus.PENDING, JobStatus.RUNNING}
            ]
        )
        seen.append(scene.worker()())
        with pytest.raises(ConflictError):
            in_unit(uow_factory, lambda uow: uow.jobs.hold(scene.receipt_id, "cli-2", JOB_LEASE))

    scene.during.check = check
    scene.run()

    assert scene.during.checked
    assert seen == [[(JobStatus.RUNNING, CLI_HOLDER)], False]
    held = scene.jobs()[-1]
    assert (held.kind, held.status, held.locked_by, held.lease_expires_at) == (
        JobKind.PROCESS_RECEIPT,
        JobStatus.SUCCEEDED,
        None,
        None,
    )
    assert scene.harness.receipt(scene.receipt_id).status is ReceiptStatus.COMPLETED


def test_a_review_answer_from_the_web_app_is_refused_while_the_cli_holds_the_receipt() -> None:
    scene = _resolve()
    uow_factory = scene.harness.uow_factory
    review_id = 1
    draft = receipt_draft(store_name="Costco", ocr_text=None, image_path=None)

    with (
        ReceiptHold(uow_factory, holder=CLI_HOLDER, renew_every_s=10.0)(scene.receipt_id),
        pytest.raises(ConflictError, match="is being processed"),
    ):
        AnswerReview(uow_factory, scene.harness.resolve)(review_id, draft)

    assert [job.kind for job in scene.jobs()] == [JobKind.PROCESS_RECEIPT]
    assert _state(scene) == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
        (ReviewStatus.OPEN,),
    )


def _failed_receipts(uow_factory: UnitOfWorkFactory, count: int) -> list[int]:
    failed = receipt(
        status=ReceiptStatus.FAILED, stage=ProcessingStage.FAILED, image_path="ab/cd/abcd.jpg"
    )
    return in_unit(
        uow_factory,
        lambda uow: [require_id(uow.receipts.add(failed).id) for _ in range(count)],
    )


def _race(*takers: Callable[[], object]) -> list[bool]:
    """Start ``takers`` at once, each on its own connection; whether each got in."""
    outcomes: list[bool] = [False] * len(takers)
    start = threading.Barrier(len(takers))

    def attempt(index: int) -> None:
        start.wait()
        try:
            takers[index]()
            outcomes[index] = True
        except ConflictError:
            pass

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(len(takers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return outcomes


@pytest.mark.parametrize("rival", ["retry", "second cli"])
def test_a_hold_racing_another_taker_lets_exactly_one_in(
    migrated_engine: Engine, rival: str
) -> None:
    uow_factory = sqlite_uow_factory(migrated_engine, FakeClock())
    hold = ReceiptHold(uow_factory, holder=CLI_HOLDER, renew_every_s=10.0)
    receipt_ids = _failed_receipts(uow_factory, ROUNDS)
    winners = []

    for receipt_id in receipt_ids:

        def cli(receipt_id: int = receipt_id) -> None:
            in_unit(uow_factory, lambda uow: hold.take(uow, receipt_id))

        def other(receipt_id: int = receipt_id) -> None:
            if rival == "retry":
                RetryReceipt(uow_factory)(receipt_id)
            else:
                in_unit(uow_factory, lambda uow: uow.jobs.hold(receipt_id, "cli-2", JOB_LEASE))

        winners.append(_race(cli, other))

    assert all(sorted(outcome) == [False, True] for outcome in winners), winners
    active = [
        job.receipt_id
        for job in _jobs(uow_factory)
        if job.status in {JobStatus.PENDING, JobStatus.RUNNING}
    ]
    assert sorted(active) == receipt_ids


def test_a_hold_is_renewed_while_it_runs_then_completed(migrated_engine: Engine) -> None:
    clock = FakeClock()
    uow_factory = sqlite_uow_factory(migrated_engine, clock)
    (receipt_id,) = _failed_receipts(uow_factory, 1)
    hold = ReceiptHold(uow_factory, holder=CLI_HOLDER, renew_every_s=0.01)

    with hold(receipt_id):
        clock.advance(timedelta(minutes=4))
        renewed = clock.now() + JOB_LEASE
        for _ in range(500):
            job = _job(uow_factory, 1)
            if job.lease_expires_at == renewed:
                break
            threading.Event().wait(0.01)
        assert (job.status, job.locked_by, job.lease_expires_at) == (
            JobStatus.RUNNING,
            CLI_HOLDER,
            renewed,
        )

    assert (_job(uow_factory, 1).status, _job(uow_factory, 1).locked_by) == (
        JobStatus.SUCCEEDED,
        None,
    )


def test_a_run_that_raises_fails_its_hold_and_the_error_goes_on(migrated_engine: Engine) -> None:
    uow_factory = sqlite_uow_factory(migrated_engine, FakeClock())
    (receipt_id,) = _failed_receipts(uow_factory, 1)
    hold = ReceiptHold(uow_factory, holder=CLI_HOLDER, renew_every_s=10.0)

    with pytest.raises(KeyboardInterrupt), hold(receipt_id):
        raise KeyboardInterrupt

    job = _job(uow_factory, 1)
    assert (job.status, job.last_error, job.locked_by) == (
        JobStatus.FAILED,
        "KeyboardInterrupt",
        None,
    )


def test_the_lapsed_hold_of_a_cli_that_died_is_failed_by_the_worker() -> None:
    scene = _resume()
    harness = scene.harness
    with harness.uow_factory() as uow:
        ReceiptHold(harness.uow_factory, holder=CLI_HOLDER, renew_every_s=10.0).take(
            uow, scene.receipt_id
        )
        uow.receipts.update_status(
            scene.receipt_id, ReceiptStatus.PROCESSING, ProcessingStage.READING
        )
        uow.commit()
    assert isinstance(harness.clock, FakeClock)
    worker = scene.worker()

    idle_while_held = worker()
    harness.clock.advance(JOB_LEASE + timedelta(seconds=1))
    idle_after = worker()

    held = scene.jobs()[-1]
    found = harness.receipt(scene.receipt_id)
    assert (idle_while_held, idle_after) == (False, False)
    assert (held.status, held.last_error) == (JobStatus.FAILED, STOPPED_UNEXPECTEDLY)
    assert (found.status, found.failure_reason) == (
        ReceiptStatus.FAILED,
        FailureReason.STOPPED_UNEXPECTEDLY,
    )
    assert scene.reader.calls == 1


def _jobs(uow_factory: UnitOfWorkFactory) -> list[Job]:
    found: list[Job] = []
    with uow_factory(read_only=True) as uow:
        while (job := uow.jobs.get(len(found) + 1)) is not None:
            found.append(job)
    return found


def _job(uow_factory: UnitOfWorkFactory, job_id: int) -> Job:
    with uow_factory(read_only=True) as uow:
        job = uow.jobs.get(job_id)
    assert job is not None
    return job
