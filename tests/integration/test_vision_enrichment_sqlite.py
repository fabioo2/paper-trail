"""The vision fallback and enrichment on a real database file (agent core slice D).

The application's tables (FTS triggers included), the sqlite-vec index and LangGraph's
``CheckpointSaver`` share one SQLite file, as in production; the LLM is scripted and the
embeddings are ``FakeEmbeddings``. Each "process" is a new harness with its own checkpointer
connection.

Covers AC-D-01 (the vision attempt's checkpoint survives a GPU drop and a new process),
AC-D-02 / S4-AC-04 (enriched products found by a tag through the real FTS index, their
vectors replaced in sqlite-vec) and slice C review note 1 (PROCESSING/MATCHING on disk while
matching waits for the GPU). Masking (AC-A-04) still holds with the vision attempt: no card
number reaches any column, checkpoint included.
"""

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from paper_trail.adapters.sqlite.checkpoints import CheckpointSaver
from paper_trail.application.process_receipt import ProcessingStoppedError
from paper_trail.domain.enums import Category, MatchSource, ProcessingStage, ReceiptStatus
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.ports.llm import LlmUnavailableError
from paper_trail.ports.search import HitKind
from tests.agent_harness import (
    VISION_MODEL,
    Harness,
    agent_settings,
    build_harness,
)
from tests.extractions import (
    COSTCO_ENRICHMENTS,
    COSTCO_OCR,
    NOW,
    TEST_PAN_DIGITS,
    extraction,
    inconsistent_extraction,
    printed_extraction,
)
from tests.fakes.clock import FakeClock
from tests.fakes.embeddings import FakeEmbeddings
from tests.fakes.images import FakeImageStore
from tests.fakes.llm import Reply, ScriptedLlm
from tests.pans import UUID, card_numbers
from tests.storage import sqlite_uow_factory, table_values
from tests.support import require_id

GPU_DOWN = "cannot reach the LLM host llm-host.test:11434"

type StartProcess = Callable[..., Harness]


@pytest.fixture
def database(migrated_engine: Engine) -> Path:
    return Path(str(migrated_engine.url.database))


@pytest.fixture
def process_on(migrated_engine: Engine, database: Path) -> Iterator[StartProcess]:
    images = FakeImageStore()
    embeddings = FakeEmbeddings()
    connections: list[sqlite3.Connection] = []

    def start(*replies: Reply, embeddings_down: bool = False) -> Harness:
        """A fresh "process" with the slice D stages on: new runner and saver connection,
        same database, images and embeddings table."""
        connection = sqlite3.connect(database, check_same_thread=False)
        connections.append(connection)
        clock = FakeClock(NOW)
        return build_harness(
            ScriptedLlm(*replies),
            settings=agent_settings(max_vision_attempts=1, enrichment=True),
            clock=clock,
            uow_factory=sqlite_uow_factory(migrated_engine, clock),
            checkpointer=CheckpointSaver(connection),
            images=images,
            embeddings=FakeEmbeddings(error=LlmUnavailableError(GPU_DOWN))
            if embeddings_down
            else embeddings,
        )

    yield start
    for connection in connections:
        connection.close()


def test_ac_d_01_a_gpu_drop_at_the_vision_attempt_resumes_in_a_new_process(
    process_on: StartProcess,
) -> None:
    first = process_on(
        inconsistent_extraction(), inconsistent_extraction(), LlmUnavailableError(GPU_DOWN)
    )
    with pytest.raises(ProcessingStoppedError) as stopped:
        first.process_image("faded")
    receipt_id = stopped.value.receipt_id
    second = process_on(printed_extraction(), *COSTCO_ENRICHMENTS)  # the vision answer

    resumed = second.resume(receipt_id)

    assert resumed.graph is not None
    assert resumed.graph.resumed_from == "supervisor"
    assert (resumed.graph.status, resumed.graph.vision_attempts) == (ReceiptStatus.COMPLETED, 1)
    assert second.llm.requests[0].model == VISION_MODEL
    assert second.match_sources(receipt_id) == [MatchSource.NEW] * 3


def test_s4_ac_04_enriched_products_are_searchable_and_their_vectors_replaced(
    process_on: StartProcess,
) -> None:
    harness = process_on(extraction(), *COSTCO_ENRICHMENTS)

    receipt_id = harness.process_text(COSTCO_OCR).receipt_id

    eggs = require_id(harness.line(receipt_id, 0).product_id)
    assert harness.product(eggs).category is Category.DAIRY_EGGS
    with harness.uow_factory(read_only=True) as uow:
        hits = uow.text_search.search("breakfast", limit=10)
        enriched = COSTCO_ENRICHMENTS[0]
        text = normalize_alias(" ".join([enriched["canonical_name"], *enriched["tags"]]))
        (nearest,) = uow.vectors.nearest(harness.embeddings.vector(text), k=1)
    assert (HitKind.PRODUCT, eggs) in [(hit.kind, hit.id) for hit in hits]
    assert nearest.product_id == eggs
    assert nearest.distance == pytest.approx(0.0, abs=1e-6)


def test_review_note_1_a_drop_during_matching_is_processing_on_disk(
    process_on: StartProcess,
) -> None:
    first = process_on(extraction(), embeddings_down=True)
    with pytest.raises(ProcessingStoppedError) as stopped:
        first.process_text(COSTCO_OCR)
    receipt_id = stopped.value.receipt_id

    stored = first.receipt(receipt_id)

    assert (stored.status, stored.stage) == (
        ReceiptStatus.PROCESSING,
        ProcessingStage.WAITING_FOR_GPU,
    )
    resumed = process_on(*COSTCO_ENRICHMENTS).resume(receipt_id)
    assert resumed.graph is not None
    assert resumed.graph.status is ReceiptStatus.COMPLETED
    done = first.receipt(receipt_id)
    assert (done.status, done.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)


def test_ac_a_04_no_card_number_reaches_the_database_with_vision_and_enrichment(
    process_on: StartProcess, database: Path
) -> None:
    harness = process_on(
        inconsistent_extraction(),
        inconsistent_extraction(),
        printed_extraction(),
        *COSTCO_ENRICHMENTS,
    )

    outcome = harness.process_image("with-a-card")

    assert outcome.graph is not None
    assert outcome.graph.vision_attempts == 1
    leaks = [
        (table, column)
        for table, column, value in table_values(database)
        if TEST_PAN_DIGITS in value.replace(" ", "") or card_numbers(UUID.sub(" ", value))
    ]
    assert leaks == []
    for request in harness.llm.requests:
        assert all(card_numbers(message.content) == [] for message in request.messages)
