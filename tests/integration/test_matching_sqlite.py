"""Matching memory and checkpointer resume on a real database file (agent core slice C).

The application's tables, the sqlite-vec index and LangGraph's ``CheckpointSaver`` share one
SQLite file, as in production; embeddings are ``FakeEmbeddings`` and the LLM is scripted. Each
"process" is a new harness with its own checkpointer connection.

Covers barcode-lookup PC-TC-65 (codes and aliases persist across processes; a correction
teaches the code), PC-TC-64 (a crash after ``save_matches`` resumed in a second process equals an
uninterrupted run) and AC-C-04 / S3-AC-08 across two processes. In-process tools; the MCP
transport is compared in ``tests/unit/agents/test_transport_equivalence.py`` (S5-AC-04).
"""

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from paper_trail.adapters.catalog.plu import PluCatalog
from paper_trail.adapters.sqlite.checkpoints import CheckpointSaver
from paper_trail.domain.enums import (
    Category,
    CodeScheme,
    CodeSource,
    FieldSource,
    MatchSource,
    ReceiptStatus,
)
from paper_trail.domain.models import ProductCode
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.tools import ToolGateway
from tests.agent_harness import Harness, agent_settings, build_harness
from tests.extractions import (
    C1,
    COSTCO_OCR,
    L4,
    LOBLAWS_KEY,
    NOW,
    PLU_CSV,
    STORE_KEY,
    U1,
    coded_extraction,
    extraction,
    one_line_extraction,
)
from tests.factories import product
from tests.fakes.clock import FakeClock
from tests.fakes.embeddings import FakeEmbeddings
from tests.fakes.images import FakeImageStore
from tests.fakes.llm import Reply, ScriptedLlm
from tests.fakes.tools import CrashAfterToolGateway
from tests.storage import sqlite_uow_factory
from tests.support import require_id

type StartProcess = Callable[..., Harness]


@pytest.fixture
def database(migrated_engine: Engine) -> Path:
    return Path(str(migrated_engine.url.database))


@pytest.fixture
def plu(tmp_path: Path) -> Path:
    path = tmp_path / "plu.csv"
    path.write_text(PLU_CSV, encoding="utf-8")
    return path


@pytest.fixture
def process_on(migrated_engine: Engine, database: Path, plu: Path) -> Iterator[StartProcess]:
    images = FakeImageStore()
    connections: list[sqlite3.Connection] = []

    def start(
        *replies: Reply,
        enrichment: bool = False,
        wrap_tools: Callable[[ToolGateway], ToolGateway] | None = None,
    ) -> Harness:
        """A fresh "process": new runner and saver connection, same database and images."""
        connection = sqlite3.connect(database, check_same_thread=False)
        connections.append(connection)
        clock = FakeClock(NOW)
        return build_harness(
            ScriptedLlm(*replies),
            settings=agent_settings(enrichment=enrichment),
            clock=clock,
            uow_factory=sqlite_uow_factory(migrated_engine, clock),
            checkpointer=CheckpointSaver(connection),
            images=images,
            embeddings=FakeEmbeddings(),
            catalog=PluCatalog(plu),
            wrap_tools=wrap_tools,
        )

    yield start
    for connection in connections:
        connection.close()


def _codes(harness: Harness) -> list[tuple[CodeScheme, str, str, int, CodeSource]]:
    with harness.uow_factory(read_only=True) as uow:
        return [
            (row.scheme, row.store_key, row.code, row.product_id, row.source)
            for row in uow.product_codes.list()
        ]


def test_pc_tc_65_codes_and_aliases_persist_across_processes(process_on: StartProcess) -> None:
    receipt = one_line_extraction(C1)
    receipt["items"] += one_line_extraction(U1)["items"]
    receipt["total_cents"] *= 2

    first = process_on(receipt).process_text(f"{C1}\n{U1}", "a.txt").receipt_id
    second_process = process_on(receipt)
    second = second_process.process_text(f"{C1}\n{U1}", "b.txt").receipt_id

    choc, eggs = (require_id(item.product_id) for item in second_process.receipt(first).items)
    assert second_process.match_sources(first) == [MatchSource.NEW, MatchSource.NEW]
    assert [(i.product_id, i.match_source) for i in second_process.receipt(second).items] == [
        (choc, MatchSource.CATALOG),
        (eggs, MatchSource.ALIAS),
    ]

    corrected = second_process.correct(second, 0, new_product_name="Choc Pistachio Bars")
    third_process = process_on(receipt)
    third = third_process.process_text(f"{C1}\n{U1}", "c.txt").receipt_id

    assert [(i.product_id, i.match_source) for i in third_process.receipt(third).items] == [
        (corrected.product_id, MatchSource.CATALOG),
        (eggs, MatchSource.ALIAS),
    ]
    assert _codes(third_process) == [
        (CodeScheme.STORE, STORE_KEY, "1362394", require_id(corrected.product_id),
         CodeSource.CORRECTION),
    ]  # fmt: skip


def test_pc_tc_64_a_second_process_resumes_after_a_crash_in_save_matches(
    process_on: StartProcess,
) -> None:
    lines = [C1, C1, L4]
    first = process_on(
        coded_extraction(lines),
        enrichment=True,
        wrap_tools=lambda gateway: CrashAfterToolGateway(gateway, "save_matches"),
    )
    crashed = first.process_text("\n".join(lines))
    assert crashed.failure is not None

    second = process_on(enrichment=True)
    resumed = second.resume(crashed.receipt_id)

    assert resumed.graph is not None
    assert resumed.graph.status is ReceiptStatus.COMPLETED
    assert second.llm.calls == 0
    choc, apple = second.product_ids()
    assert [(i.product_id, i.match_source) for i in second.receipt(crashed.receipt_id).items] == [
        (choc, MatchSource.NEW),
        (choc, MatchSource.NEW),
        (apple, MatchSource.NEW),
    ]
    printed, organic = second.product(choc), second.product(apple)
    assert (printed.canonical_name, dict(printed.field_sources), printed.category) == (
        "CHOC PISTACH",
        {"canonical_name": FieldSource.RECEIPT_TEXT},
        None,
    )
    assert (
        organic.canonical_name,
        organic.category,
        organic.tags,
        dict(organic.field_sources),
    ) == (
        "Organic Test Apple",
        Category.PRODUCE,
        ("organic",),
        {"canonical_name": FieldSource.PLU, "category": FieldSource.PLU, "tags": FieldSource.PLU},
    )
    assert _codes(second) == [
        (CodeScheme.STORE, LOBLAWS_KEY, "1362394", choc, CodeSource.RECEIPT),
        (CodeScheme.PLU, "", "93101", apple, CodeSource.PLU),
    ]
    assert [len(second.price_history(pid)) for pid in (choc, apple)] == [2, 1]
    assert (second.has_vector(choc), second.has_vector(apple)) == (False, True)
    describes = [
        event
        for harness in (first, second)
        for event in harness.events(crashed.receipt_id)
        if event.kind is TraceKind.TOOL and event.name == "describe_from_catalog"
    ]
    assert len(describes) == 1


def test_pc_tc_64_a_persons_remap_before_the_resume_wins(process_on: StartProcess) -> None:
    """Codex P1-b: after the crash a person makes the code name another product; the resumed
    run links the line there (``catalog``) with its price, and the product the first run
    created is neither priced nor described."""
    first = process_on(
        coded_extraction([L4]),
        enrichment=True,
        wrap_tools=lambda gateway: CrashAfterToolGateway(gateway, "save_matches"),
    )
    crashed = first.process_text(L4)
    assert crashed.failure is not None
    (created,) = first.product_ids()
    second = process_on(enrichment=True)
    with second.uow_factory() as uow:
        person = require_id(uow.products.add(product(canonical_name="Organic Apples")).id)
        uow.product_codes.upsert(
            ProductCode(None, person, CodeScheme.PLU, "", "93101", CodeSource.MANUAL, NOW, NOW),
            replace=True,
        )
        uow.commit()

    resumed = second.resume(crashed.receipt_id)

    assert resumed.graph is not None
    assert resumed.graph.status is ReceiptStatus.COMPLETED
    line = second.receipt(crashed.receipt_id).items[0]
    assert (line.product_id, line.match_source) == (person, MatchSource.CATALOG)
    assert len(second.price_history(person)) == 1
    assert second.price_history(created) == ()
    assert dict(second.product(created).field_sources) == {"canonical_name": FieldSource.PLU}
    assert second.product(created).category is None
    describes = [
        event
        for event in second.events(crashed.receipt_id)
        if event.kind is TraceKind.TOOL and event.name == "describe_from_catalog"
    ]
    assert describes == []


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
