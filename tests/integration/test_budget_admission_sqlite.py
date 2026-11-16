"""Budget admission across processes on one SQLite file (claude-extraction review round 1, R4).

Two ``BudgetedReader`` over two engines on the same database stand for two processes. Pinned:
admission is serialized and counts the calls still in flight (each reserved at its maximum
cost), so when the spend is just under the cap only one of two overlapping calls starts: the
month can overshoot the cap by at most one call. After the call its reservation becomes the
real cost; a call that fails unbilled releases it.
"""

import io
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx2
import pytest
from PIL import Image
from sqlalchemy import Engine, select

from paper_trail.adapters.anthropic.reader import AnthropicReader
from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.schema import claude_calls
from paper_trail.adapters.sqlite.spend_ledger import SqliteSpendLedger
from paper_trail.agents.schemas import ReceiptAnswer
from paper_trail.application.budget import BudgetedReader
from paper_trail.config import DATABASE_FILENAME, ClaudeSettings
from paper_trail.domain.rules.budget import call_ceiling_usd, month_start
from paper_trail.ports.budget import ClaudeCall
from paper_trail.ports.reader import (
    BudgetReachedError,
    ReaderUnavailableError,
    ReadRequest,
    ReadResult,
    ReadUsage,
)
from tests.anthropic_api import CannedApi, Reply, error_response, message
from tests.extractions import answer
from tests.fakes.clock import FakeClock
from tests.support import CopyMigrated

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
CAP = Decimal("5.00")
OPUS = "claude-opus-5-5"
# 1,500 in x $4 + 700 out x $20 per MTok: what one Opus read really costs.
OPUS_READ_COST = Decimal("0.020")
USAGE = ReadUsage(input_tokens=1500, output_tokens=700)
WAIT_S = 10
# CLAUDE__TIMEOUT_S (default 300) x 3 attempts (the SDK's 2 retries) + 120 s of backoff
# margin (review R15, cold-review nit).
ABANDONED_AFTER = timedelta(seconds=300 * 3 + 120)


class BlockingReader:
    """A reader that holds each call open until ``release`` is set; ``error`` is raised
    instead of answering."""

    def __init__(self, error: Exception | None = None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._error = error

    def read[M](self, request: ReadRequest, schema: type[M]) -> ReadResult[M]:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(WAIT_S), "the test never released the call"
        if self._error is not None:
            raise self._error
        return ReadResult(value=cast(M, None), usage=USAGE, stop_reason="end_turn")


def _request() -> ReadRequest:
    return ReadRequest(
        model=OPUS, tag="extraction/test", system="", text="", image=b"photo", max_tokens=16000
    )


@pytest.fixture
def database(tmp_path: Path, copy_migrated: CopyMigrated) -> Path:
    return copy_migrated(tmp_path / DATABASE_FILENAME)


@pytest.fixture
def engines(database: Path) -> Iterator[tuple[Engine, Engine]]:
    first, second = create_engine(database), create_engine(database)
    try:
        yield first, second
    finally:
        first.dispose()
        second.dispose()


def _ledger(engine: Engine) -> SqliteSpendLedger:
    """The ledger at ``NOW``: whether a reservation is abandoned depends on its clock."""
    return SqliteSpendLedger(engine, clock=FakeClock(NOW), abandon_after=ABANDONED_AFTER)


def _budgeted(engine: Engine, inner: BlockingReader) -> BudgetedReader:
    return BudgetedReader(inner, _ledger(engine), FakeClock(NOW), CAP)


def _seed(engine: Engine, cost: str) -> None:
    _ledger(engine).record(
        ClaudeCall(at=NOW, model=OPUS, input_tokens=0, output_tokens=0, cost_usd=Decimal(cost))
    )


def _spent(engine: Engine) -> Decimal:
    return _ledger(engine).spent_since(month_start(NOW))


def _in_thread(reader: BudgetedReader, failures: list[BaseException]) -> threading.Thread:
    def run() -> None:
        try:
            reader.read(_request(), ReceiptAnswer)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_r4_only_one_of_two_overlapping_calls_starts_just_under_the_cap(
    engines: tuple[Engine, Engine],
) -> None:
    first_engine, second_engine = engines
    _seed(first_engine, "4.99")
    first, second = BlockingReader(), BlockingReader()
    second.release.set()
    failures: list[BaseException] = []

    thread = _in_thread(_budgeted(first_engine, first), failures)
    assert first.entered.wait(WAIT_S)
    try:
        with pytest.raises(BudgetReachedError):
            _budgeted(second_engine, second).read(_request(), ReceiptAnswer)
    finally:
        first.release.set()
        thread.join(WAIT_S)

    assert (failures, first.calls, second.calls) == ([], 1, 0)
    assert _spent(second_engine) == Decimal("4.99") + OPUS_READ_COST


def test_r4_a_reservation_becomes_the_real_cost(engines: tuple[Engine, Engine]) -> None:
    first_engine, second_engine = engines
    inner = BlockingReader()
    inner.release.set()

    _budgeted(first_engine, inner).read(_request(), ReceiptAnswer)

    assert _spent(second_engine) == OPUS_READ_COST


def test_r4_an_unbilled_failure_releases_its_reservation(
    engines: tuple[Engine, Engine],
) -> None:
    first_engine, second_engine = engines
    _seed(first_engine, "4.99")
    failing = BlockingReader(ReaderUnavailableError("the Claude API is unreachable"))
    failing.release.set()

    with pytest.raises(ReaderUnavailableError):
        _budgeted(first_engine, failing).read(_request(), ReceiptAnswer)
    after = BlockingReader()
    after.release.set()
    _budgeted(second_engine, after).read(_request(), ReceiptAnswer)

    assert (after.calls, _spent(second_engine)) == (1, Decimal("4.99") + OPUS_READ_COST)


# --- R15: an abandoned reservation (its process died mid-call) stops counting -----------------


def _reserve_at(engine: Engine, at: datetime, cost: str) -> None:
    reservation = _ledger(engine).reserve(
        at, OPUS, Decimal(cost), since=month_start(at), cap_usd=Decimal(1_000)
    )
    assert reservation is not None


@pytest.mark.parametrize(
    ("age", "admitted"),
    [
        (ABANDONED_AFTER + timedelta(seconds=1), True),
        (ABANDONED_AFTER - timedelta(seconds=1), False),
    ],
    ids=["abandoned", "still-in-flight"],
)
def test_r15_only_a_fresh_reservation_blocks_admission(
    engines: tuple[Engine, Engine], age: timedelta, admitted: bool
) -> None:
    first_engine, second_engine = engines
    _reserve_at(first_engine, NOW - age, "5.00")
    inner = BlockingReader()
    inner.release.set()
    reader = BudgetedReader(inner, _ledger(second_engine), FakeClock(NOW), CAP)

    if admitted:
        reader.read(_request(), ReceiptAnswer)
    else:
        with pytest.raises(BudgetReachedError):
            reader.read(_request(), ReceiptAnswer)

    assert inner.calls == (1 if admitted else 0)


def test_r15_an_abandoned_reservation_is_not_counted_in_the_spend(
    engines: tuple[Engine, Engine],
) -> None:
    first_engine, second_engine = engines
    _seed(first_engine, "5.00")
    _reserve_at(first_engine, NOW - ABANDONED_AFTER - timedelta(seconds=1), "3.00")
    reader = BudgetedReader(BlockingReader(), _ledger(second_engine), FakeClock(NOW), CAP)

    with pytest.raises(BudgetReachedError, match=r"spent 5\.00 of 5\.00 USD"):
        reader.read(_request(), ReceiptAnswer)


def test_settling_keeps_the_reservations_time(engines: tuple[Engine, Engine]) -> None:
    """A call reserved before midnight on the last day of the month stays in that month."""
    first_engine, _ = engines
    reserved_at = NOW - timedelta(minutes=10)
    ledger = _ledger(first_engine)
    reservation = ledger.reserve(
        reserved_at, OPUS, Decimal("0.50"), since=month_start(reserved_at), cap_usd=CAP
    )
    assert reservation is not None

    ledger.settle(reservation, ClaudeCall(NOW, OPUS, 1500, 700, OPUS_READ_COST))

    with first_engine.connect() as connection:
        stored = connection.execute(select(claude_calls.c.at, claude_calls.c.cost_usd)).all()
    assert [tuple(row) for row in stored] == [(reserved_at, OPUS_READ_COST)]


# --- C2, C11: each attempt is reserved, then settled (maybe billed) or released (unbilled) ----


def _photo_request() -> ReadRequest:
    out = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(out, format="JPEG")
    return replace(_request(), image=out.getvalue())


def _budgeted_api(engine: Engine, api: CannedApi) -> BudgetedReader:
    """As production builds it: the SDK never retries (C11), ``BudgetedReader`` does."""
    reader = AnthropicReader(ClaudeSettings(), client=api.client(max_retries=0))
    return BudgetedReader(reader, _ledger(engine), FakeClock(NOW), CAP)


CEILING = call_ceiling_usd(OPUS, 0, 16000)
RETRYABLE: dict[str, Callable[[], Reply]] = {
    "connection-refused": lambda: httpx2.ConnectError("connection refused"),
    "429": lambda: error_response(429, "rate_limit_error", "slow down"),
    "500": lambda: error_response(500, "api_error", "internal"),
    "529": lambda: error_response(529, "overloaded_error", "busy"),
}


@pytest.mark.parametrize("reply", RETRYABLE.values(), ids=RETRYABLE)
def test_c11_a_retryable_unbilled_failure_is_tried_3_times_and_costs_nothing(
    engines: tuple[Engine, Engine], reply: Callable[[], Reply]
) -> None:
    first_engine, second_engine = engines
    api = CannedApi(then=reply())

    with pytest.raises(ReaderUnavailableError):
        _budgeted_api(first_engine, api).read(_photo_request(), ReceiptAnswer)

    assert (len(api.requests), _spent(second_engine)) == (3, Decimal(0))


def test_c11_a_bad_request_is_not_retried(engines: tuple[Engine, Engine]) -> None:
    first_engine, second_engine = engines
    api = CannedApi(then=error_response(400, "invalid_request_error", "bad request"))

    with pytest.raises(ReaderUnavailableError):
        _budgeted_api(first_engine, api).read(_photo_request(), ReceiptAnswer)

    assert (len(api.requests), _spent(second_engine)) == (1, Decimal(0))


def test_c2_c11_a_timeout_is_charged_its_ceiling_once_per_attempt(
    engines: tuple[Engine, Engine],
) -> None:
    """The API may have answered (and billed) a request the client stopped waiting for: each
    of the 3 attempts settles at its own ceiling (C11 replaces C2's x 3 on one reservation)."""
    first_engine, second_engine = engines
    api = CannedApi(then=httpx2.ReadTimeout("timed out"))

    with pytest.raises(ReaderUnavailableError):
        _budgeted_api(first_engine, api).read(_photo_request(), ReceiptAnswer)

    assert (len(api.requests), _spent(second_engine)) == (3, CEILING * 3)


def test_c11_timeout_then_500_then_an_answer_costs_ceiling_plus_nothing_plus_the_answer(
    engines: tuple[Engine, Engine],
) -> None:
    first_engine, second_engine = engines
    api = CannedApi(
        httpx2.ReadTimeout("timed out"),
        error_response(500, "api_error", "internal"),
        message(answer(), model=OPUS),
    )

    _budgeted_api(first_engine, api).read(_photo_request(), ReceiptAnswer)

    assert (len(api.requests), _spent(second_engine)) == (3, CEILING + OPUS_READ_COST)
