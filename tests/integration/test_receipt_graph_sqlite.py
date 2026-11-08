"""The receipt graph on a real database file with LangGraph's SqliteSaver beside the app's tables.

Covers AC-A-04 (no card number in any column or checkpoint blob), AC-A-08 / S3-AC-07 (a review
resolved by a *second* process resumes the same thread from the checkpoint) and the plan's risk
"SqliteSaver + app writes on one DB file".

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

import pytest
from sqlalchemy import Engine

from paper_trail.adapters.sqlite.checkpoints import CheckpointSaver
from paper_trail.domain.enums import ReceiptStatus, ReviewStatus
from tests.agent_harness import (
    Harness,
    build_harness,
)
from tests.extractions import NOW, TEST_PAN_DIGITS, extraction, inconsistent_extraction
from tests.factories import receipt_draft
from tests.fakes.clock import FakeClock
from tests.fakes.images import FakeImageStore
from tests.fakes.llm import ScriptedLlm
from tests.pans import UUID, card_numbers
from tests.storage import sqlite_uow_factory, table_values
from tests.support import require_id

type StartProcess = Callable[[ScriptedLlm], Harness]


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

    def start(llm: ScriptedLlm) -> Harness:
        """A fresh "process": new runner, new saver connection, same database and images."""
        connection = sqlite3.connect(database, check_same_thread=False)
        connections.append(connection)
        clock = FakeClock(NOW)
        return build_harness(
            llm,
            clock=clock,
            uow_factory=sqlite_uow_factory(migrated_engine, clock),
            checkpointer=CheckpointSaver(connection),
            images=images,
        )

    yield start


def _parked_for_review(start: StartProcess) -> tuple[Harness, int, int]:
    harness = start(ScriptedLlm(then=inconsistent_extraction()))
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


def test_ac_a_04_no_card_number_in_any_column_or_checkpoint(
    process_on: StartProcess, database: Path
) -> None:
    """AC-A-04: OCR read VISA 4111 1111 1111 1111; after a run parked for review (its state
    checkpointed) and after the review is resolved, no table, checkpoint blob included, holds a
    Luhn-valid 13-19 digit run; neither does any LLM request or trace event."""
    harness, receipt_id, review_id = _parked_for_review(process_on)

    assert _threads(database) == {f"receipt-{receipt_id}"}
    assert _leaks(database) == []
    harness.resolve(review_id, receipt_draft(ocr_text=None, image_path=None))
    assert _leaks(database) == []
    for request in harness.llm.requests:
        assert all(card_numbers(message.content) == [] for message in request.messages)
    for event in harness.events(receipt_id):
        assert card_numbers(f"{event.summary} {event.data!r}") == []
    stored = harness.receipt(receipt_id)
    assert stored.ocr_text is not None
    assert TEST_PAN_DIGITS not in stored.ocr_text.replace(" ", "")


def test_ac_a_08_a_second_process_resumes_the_same_thread(process_on: StartProcess) -> None:
    """AC-A-08 / S3-AC-07: `review resolve` runs in another process than `process`; it resumes
    the checkpointed thread (no extraction call) and completes the receipt."""
    first, receipt_id, review_id = _parked_for_review(process_on)
    second = process_on(ScriptedLlm())

    pending = second.runner.pending_draft(receipt_id)
    receipt = second.resolve(review_id, receipt_draft(ocr_text=None, image_path=None))

    assert pending is not None
    assert pending.items[2].line_total.amount_cents == 989
    assert second.llm.calls == 0
    assert receipt.status is ReceiptStatus.COMPLETED
    assert second.review(review_id).status is ReviewStatus.RESOLVED
    assert first.llm.calls == 2


def test_a_clean_run_on_sqlite_completes(process_on: StartProcess, database: Path) -> None:
    harness = process_on(ScriptedLlm(extraction()))

    graph = harness.process_image().graph

    assert graph is not None
    assert graph.status is ReceiptStatus.COMPLETED
    assert harness.receipt(graph.receipt_id).items
    assert _threads(database) == {f"receipt-{graph.receipt_id}"}
