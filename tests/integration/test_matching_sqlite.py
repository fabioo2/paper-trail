"""Matching memory and checkpointer resume on a real database file (agent core slice C).

The application's tables, the sqlite-vec index and LangGraph's ``CheckpointSaver`` share one
SQLite file, as in production; embeddings are ``FakeEmbeddings`` and the LLM is scripted. Each
"process" is a new harness with its own checkpointer connection.

Covers AC-C-03 (new -> alias -> memory) and S4-AC-02 through the real vector index, and
AC-C-04 / S3-AC-08 across two processes. In-process tools; the MCP transport is compared in
``tests/unit/agents/test_transport_equivalence.py`` (S5-AC-04).
"""

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from paper_trail.adapters.sqlite.checkpoints import CheckpointSaver
from paper_trail.domain.enums import MatchSource, ReceiptStatus
from tests.agent_harness import (
    Harness,
    build_harness,
)
from tests.extractions import (
    COSTCO_OCR,
    EGGS_ALIAS,
    KIRKLAND_EGGS,
    KIRKLAND_EGGS_ALIAS,
    NOW,
    STORE_KEY,
    WALMART,
    WALMART_KEY,
    extraction,
    one_line_extraction,
)
from tests.fakes.clock import FakeClock
from tests.fakes.embeddings import FakeEmbeddings, at_distance, basis
from tests.fakes.images import FakeImageStore
from tests.fakes.llm import Reply, ScriptedLlm
from tests.storage import sqlite_uow_factory
from tests.support import require_id

TABLE = {EGGS_ALIAS: basis(0), KIRKLAND_EGGS_ALIAS: at_distance(0.05)}

type StartProcess = Callable[..., Harness]


@pytest.fixture
def database(migrated_engine: Engine) -> Path:
    return Path(str(migrated_engine.url.database))


@pytest.fixture
def process_on(migrated_engine: Engine, database: Path) -> Iterator[StartProcess]:
    images = FakeImageStore()
    connections: list[sqlite3.Connection] = []

    def start(*replies: Reply) -> Harness:
        """A fresh "process": new runner and saver connection, same database and images."""
        connection = sqlite3.connect(database, check_same_thread=False)
        connections.append(connection)
        clock = FakeClock(NOW)
        return build_harness(
            ScriptedLlm(*replies),
            clock=clock,
            uow_factory=sqlite_uow_factory(migrated_engine, clock),
            checkpointer=CheckpointSaver(connection),
            images=images,
            embeddings=FakeEmbeddings(TABLE),
        )

    yield start
    for connection in connections:
        connection.close()


def test_ac_c_03_and_s4_ac_02_memory_on_sqlite(process_on: StartProcess) -> None:
    harness = process_on(
        extraction(), extraction(), one_line_extraction(KIRKLAND_EGGS, store_name=WALMART)
    )

    first = harness.process_text(COSTCO_OCR, "a.txt").receipt_id
    second = harness.process_text(COSTCO_OCR, "b.txt").receipt_id
    walmart = harness.process_text(f"{WALMART}\n{KIRKLAND_EGGS} 8.99", "w.txt").receipt_id

    assert harness.match_sources(first) == [MatchSource.NEW] * 3
    assert harness.match_sources(second) == [MatchSource.ALIAS] * 3
    eggs = require_id(harness.line(first, 0).product_id)
    assert harness.has_vector(eggs)
    line = harness.line(walmart, 0)
    assert (line.product_id, line.match_source) == (eggs, MatchSource.VECTOR)
    stores = sorted(price.store_key for price in harness.price_history(eggs))
    assert stores == [STORE_KEY, STORE_KEY, WALMART_KEY]

    corrected = harness.correct(second, 0, new_product_name="Kirkland Organic Eggs 24")
    third = process_on(extraction()).process_text(COSTCO_OCR, "c.txt").receipt_id

    assert harness.line(third, 0).product_id == corrected.product_id
    assert harness.match_sources(third) == [
        MatchSource.MEMORY,
        MatchSource.ALIAS,
        MatchSource.ALIAS,
    ]


def test_ac_c_04_a_second_process_resumes_after_the_breakpoint(process_on: StartProcess) -> None:
    first = process_on(extraction())
    paused = first.process_text(COSTCO_OCR, interrupt_after="extract_text")
    assert paused.graph is not None
    assert paused.graph.paused_at == "extract_text"

    second = process_on()
    resumed = second.resume(paused.receipt_id)

    assert resumed.graph is not None
    assert resumed.graph.status is ReceiptStatus.COMPLETED
    assert resumed.graph.resumed_from == "extract_text"
    assert second.llm.calls == 0
    assert second.match_sources(paused.receipt_id) == [MatchSource.NEW] * 3
