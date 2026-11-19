"""The receipt graph on a real database file with LangGraph's SqliteSaver beside the app's tables.

Covers AC-A-08 / S3-AC-07 (a review resolved by a *second* process resumes the same thread from
the checkpoint), CE-TC-027 across processes (a run stopped by Claude being unreachable is
continued by a new process on the retry model, not started over), the plan's risk "SqliteSaver +
app writes on one DB file" and CE-TC-032 (card numbers in Claude's answer are masked before they
are checkpointed, stored or traced; the photo itself is sent as is, claude-extraction plan V4,
E6).

Pinned (docs/tests/agent-core-A.md): the checkpointer is ``SqliteSaver`` on the application's
database file with its own connection; threads are named ``receipt-<id>``. It is the production
subclass ``CheckpointSaver``: the stock saver's random channel-version suffixes are sixteen
digits that the card-number scan below cannot tell from a leak.

In-process tools; the MCP transport is compared in
``tests/unit/agents/test_transport_equivalence.py`` (S5-AC-04, agent core slice B).
"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from paper_trail.adapters.sqlite.checkpoints import CheckpointSaver
from paper_trail.domain.enums import (
    LineKind,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
    ReviewStatus,
)
from paper_trail.domain.paging import PageRequest
from paper_trail.ports.reader import ReaderError, ReaderUnavailableError
from tests.agent_harness import RETRY_MODEL, Harness, agent_settings, build_harness
from tests.extractions import (
    NOW,
    TEST_PAN_DIGITS,
    adjustment,
    answer,
    inconsistent_answer,
)
from tests.factories import MASKED_TEST_PAN, TEST_PAN, receipt_draft
from tests.fakes.clock import FakeClock
from tests.fakes.images import FakeImageStore
from tests.fakes.reader import ScriptedReader
from tests.pans import UUID, card_numbers
from tests.storage import sqlite_uow_factory, table_values
from tests.support import require_id
from tests.unit.agents.graph_support import raw_traced

type StartProcess = Callable[..., Harness]


@pytest.fixture
def database(migrated_engine: Engine) -> Path:
    return Path(str(migrated_engine.url.database))


@pytest.fixture
def connections(database: Path) -> Iterator[list[sqlite3.Connection]]:
    """Checkpointer connections, one per simulated process, closed at the end."""
    opened: list[sqlite3.Connection] = []
    yield opened
    for connection in opened:
        connection.close()


@pytest.fixture
def process_on(
    migrated_engine: Engine, database: Path, connections: list[sqlite3.Connection]
) -> Iterator[StartProcess]:
    images = FakeImageStore()

    def start(reader: ScriptedReader, **overrides: Any) -> Harness:
        """A fresh "process": new runner, new saver connection, same database and images."""
        connection = sqlite3.connect(database, check_same_thread=False)
        connections.append(connection)
        clock = FakeClock(NOW)
        return build_harness(
            reader,
            clock=clock,
            uow_factory=sqlite_uow_factory(migrated_engine, clock),
            checkpointer=CheckpointSaver(connection),
            images=images,
            **overrides,
        )

    yield start


def _parked_for_review(start: StartProcess) -> tuple[Harness, int, int]:
    harness = start(ScriptedReader(then=inconsistent_answer()))
    graph = harness.process_image().graph
    assert graph is not None
    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    return harness, graph.receipt_id, require_id(graph.review_id)


def _threads(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
    return {row[0] for row in rows}


def _leaks(database: Path) -> list[tuple[str, str, str]]:
    return [cell for cell in table_values(database) if card_numbers(UUID.sub(" ", cell[2]))]


def _carded(reading: dict[str, Any]) -> dict[str, Any]:
    """``reading`` with the test card number printed in the store name, the first item's text
    and name and a deposit's text (+0.10 on every sum), paid with a card ending 4242."""
    eggs = reading["items"][0]
    eggs["printed_text"] = f"{eggs['printed_text']} {TEST_PAN}"
    eggs["name"] = f"Eggs paid with {TEST_PAN_DIGITS}"
    reading["store_name"] = f"{reading['store_name']} {TEST_PAN_DIGITS}"
    reading["adjustments"] = [adjustment("deposit", f"DEPOSIT {TEST_PAN}", "0.10")]
    reading["subtotal"] = "25.65"
    reading["total"] = "26.93"
    reading["payment_last4"] = "4242"
    return reading


def _assert_masked(harness: Harness, database: Path, receipt_id: int) -> None:
    """The harness's tracer masks nothing (``raw_traced``): a card number in an event is a
    leak of the code that emitted it."""
    assert _leaks(database) == []
    for event in harness.events(receipt_id):
        assert card_numbers(f"{event.summary} {event.data!r}") == []
    assert TEST_PAN_DIGITS not in database.read_bytes().decode("latin-1").replace(" ", "")


def test_ce_tc_032_a_card_number_in_claudes_answer_is_masked_everywhere_it_is_kept(
    process_on: StartProcess, database: Path
) -> None:
    """CE-TC-032: a completed run stores the masked texts and the printed last four only."""
    harness = raw_traced(process_on(ScriptedReader(_carded(answer()))))

    graph = harness.process_image().graph

    assert graph is not None
    assert graph.status is ReceiptStatus.COMPLETED
    stored = harness.receipt(graph.receipt_id)
    assert stored.payment_last4 == "4242"
    assert [(item.kind, item.raw_text) for item in stored.items][0::3] == [
        (LineKind.ITEM, f"KS ORG EGGS 24CT {MASKED_TEST_PAN}"),
        (LineKind.DEPOSIT, f"DEPOSIT {MASKED_TEST_PAN}"),
    ]
    assert card_numbers(repr(stored)) == []
    _assert_masked(harness, database, graph.receipt_id)


def test_ce_tc_032_a_carded_answer_parked_for_review_is_masked_in_the_checkpoint_and_review(
    process_on: StartProcess, database: Path
) -> None:
    """CE-TC-032: the failed reading is checkpointed and copied into the review item masked;
    so is everything after the person resolves it."""
    carded = _carded(inconsistent_answer())
    harness = raw_traced(
        process_on(ScriptedReader(carded), settings=agent_settings(max_attempts=1))
    )
    graph = harness.process_image().graph
    assert graph is not None
    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    review_id = require_id(graph.review_id)

    assert _threads(database) == {f"receipt-{graph.receipt_id}"}
    assert card_numbers(repr(harness.review(review_id).details)) == []
    _assert_masked(harness, database, graph.receipt_id)
    harness.resolve(review_id, force=True, reason="checked against the paper")
    _assert_masked(harness, database, graph.receipt_id)


def test_ac_a_08_a_second_process_resumes_the_same_thread(process_on: StartProcess) -> None:
    """AC-A-08 / S3-AC-07: `review resolve` runs in another process than `process`; it resumes
    the checkpointed thread (no Claude call) and completes the receipt."""
    first, receipt_id, review_id = _parked_for_review(process_on)
    reader = ScriptedReader()
    second = process_on(reader)

    pending = second.runner.pending_draft(receipt_id)
    receipt = second.resolve(
        review_id,
        receipt_draft(ocr_text=None, image_path=None),
        force=True,
        reason="checked the paper",
    )

    assert pending is not None
    assert pending.items[2].line_total.amount_cents == 989
    assert reader.calls == 0
    assert receipt.status is ReceiptStatus.COMPLETED
    assert second.review(review_id).status is ReviewStatus.RESOLVED
    first_reader = first.reader
    assert isinstance(first_reader, ScriptedReader)
    assert first_reader.calls == 2


def test_ce_tc_027_a_new_process_resumes_a_stopped_run_on_the_retry_model(
    process_on: StartProcess,
) -> None:
    first = process_on(ScriptedReader(inconsistent_answer(), ReaderUnavailableError("down")))
    with pytest.raises(ReaderError):
        first.process_image("faded")
    with first.uow_factory(read_only=True) as uow:
        (failed,) = uow.receipts.list(PageRequest(limit=10)).items
    assert failed.status is ReceiptStatus.FAILED
    assert first.receipt(failed.id).stage is ProcessingStage.FAILED
    assert first.product_ids() == []
    reader = ScriptedReader(answer())
    second = process_on(reader)

    resumed = second.resume(failed.id)

    assert resumed.graph is not None
    assert resumed.graph.status is ReceiptStatus.COMPLETED
    assert reader.models == [RETRY_MODEL]
    assert second.match_sources(failed.id) == [MatchSource.NEW] * 3


def test_a_clean_run_on_sqlite_completes(process_on: StartProcess, database: Path) -> None:
    harness = process_on(ScriptedReader(answer()))

    graph = harness.process_image().graph

    assert graph is not None
    assert graph.status is ReceiptStatus.COMPLETED
    assert harness.receipt(graph.receipt_id).items
    assert _threads(database) == {f"receipt-{graph.receipt_id}"}
