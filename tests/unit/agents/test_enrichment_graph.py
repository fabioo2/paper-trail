"""Enrichment by lookup only through the whole graph (barcode-lookup plan PR 1a: R9, A1, A6;
PC-TC-66..71). Before PR 1a this stage asked the model for a name, brand, size and category;
now no model is asked anywhere after the extraction.

Scripted reader holding the extraction answer only (any further read fails the run), the PLU
table on a ``tmp_path`` file (``PluCatalog``, the production adapter: it tells an organic code
from a conventional one), in-process tools; the MCP transport is compared in
``test_transport_equivalence.py``.

Pinned (plan R9, A6; signatures "Agents"):
- ``GRAPH__ENRICHMENT`` (default true) turns the stage on: ``commit_matches -> enrich ->
  commit_enrichment``. ``enrich`` is the ENRICHMENT agent and calls no tool and no model: for the
  first created line of each product it turns the ``catalog`` entry matching found
  (``state["created"]``) into ``describe_from_catalog`` input; a product without an entry is
  left as matching made it. ``commit_enrichment`` (SUPERVISOR) calls ``describe_from_catalog``
  once per described product with the line's raw text as ``alias`` and the receipt's store key.
- A PLU entry supplies name, category produce and, for an organic code, the tag ``organic``,
  each with source ``plu``. A person's CATEGORY correction for the line's alias and store wins
  (source ``person``), whether or not a source knows the product and whether or not the stage
  runs (TQ11).
- Only products this receipt created are described; a crash in ``commit_enrichment`` resumes
  with the describes only.
"""

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from paper_trail.adapters.catalog.plu import PluCatalog
from paper_trail.agents.graph import NODE_NAMES
from paper_trail.agents.state import thread_id
from paper_trail.application.process_receipt import ProcessOutcome
from paper_trail.domain.enums import (
    AgentRole,
    Category,
    CorrectionKind,
    FieldSource,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
)
from paper_trail.domain.models import JsonValue, Product
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.domain.trace import TraceEvent, TraceKind
from paper_trail.ports.tools import ToolGateway
from tests.agent_harness import Harness, agent_settings, build_harness
from tests.extractions import (
    C1,
    L3,
    L4,
    LOBLAWS,
    LOBLAWS_KEY,
    NOW,
    PLU_CSV,
    STORE_KEY,
    STORE_NAME,
    U1,
    coded_answer,
)
from tests.factories import correction
from tests.fakes.reader import Reply, ScriptedReader
from tests.fakes.tools import CrashAfterToolGateway, MisroutedToolGateway, RecordingToolGateway
from tests.storage import in_unit
from tests.support import require_id
from tests.unit.agents.graph_support import fresh_label, scripted

LLM = TraceKind.LLM
TOOL = TraceKind.TOOL
DESCRIBE = "describe_from_catalog"
PLU = FieldSource.PLU
PERSON = FieldSource.PERSON
RECEIPT_TEXT = FieldSource.RECEIPT_TEXT

TEST_APPLE_ENTRY: dict[str, JsonValue] = {
    "source": "plu",
    "key": "3101",
    "name": "Test Apple",
    "brand": None,
    "size_value": None,
    "size_unit": None,
    "quantity_text": None,
    "categories": [],
    "category": "produce",
    "tags": [],
}


@pytest.fixture
def plu(tmp_path: Path) -> PluCatalog:
    path = tmp_path / "catalog" / "plu.csv"
    path.parent.mkdir()
    path.write_text(PLU_CSV, encoding="utf-8")
    return PluCatalog(path)


def _harness(
    plu: PluCatalog,
    *replies: Reply,
    enrichment_on: bool = True,
    harness: Harness | None = None,
    wrap_tools: Any = None,
) -> Harness:
    """A process on a fresh database, or on ``harness``'s data (a second process)."""
    if harness is None:
        return build_harness(
            ScriptedReader(*replies),
            settings=agent_settings(enrichment=enrichment_on),
            catalog=plu,
            wrap_tools=wrap_tools,
        )
    return build_harness(
        ScriptedReader(*replies),
        settings=harness.settings,
        checkpointer=harness.checkpointer,
        uow_factory=harness.uow_factory,
        images=harness.images,
        catalog=plu,
        wrap_tools=wrap_tools,
    )


def _completed(outcome: ProcessOutcome) -> int:
    assert outcome.failure is None, outcome.failure
    assert outcome.graph is not None
    assert outcome.graph.status is ReceiptStatus.COMPLETED
    return outcome.receipt_id


def _process(harness: Harness) -> int:
    return _completed(harness.process_image(fresh_label()))


def _products(harness: Harness, receipt_id: int) -> list[Product]:
    return [
        harness.product(require_id(item.product_id)) for item in harness.receipt(receipt_id).items
    ]


def _events(harness: Harness, receipt_id: int, kind: TraceKind, name: str) -> list[TraceEvent]:
    return [
        event for event in harness.events(receipt_id) if event.kind is kind and event.name == name
    ]


def _assert_no_model_after_extraction(harness: Harness, receipt_id: int) -> None:
    """Zero model requests (signatures "Tests"): the extraction is the only read."""
    assert scripted(harness).calls == 1
    asked = [
        event
        for event in harness.events(receipt_id)
        if event.kind is LLM and event.agent in {AgentRole.MATCHING, AgentRole.ENRICHMENT}
    ]
    assert asked == []


def _product(
    product_id: int | None,
    name: str,
    sources: dict[str, FieldSource],
    *,
    category: Category | None = None,
    tags: tuple[str, ...] = (),
) -> Product:
    return Product(product_id, name, None, None, None, category, tags, NOW, sources)


def test_enrich_and_commit_enrichment_are_nodes_of_the_graph() -> None:
    assert NODE_NAMES[-3:] == ("commit_matches", "enrich", "commit_enrichment")


# --- PC-TC-66: a PLU hit fills what the source supplies ------------------------------------------


@pytest.mark.parametrize(
    ("raw_text", "expected"),
    [
        (
            L4,
            (
                "Organic Test Apple",
                Category.PRODUCE,
                ("organic",),
                {"canonical_name": PLU, "category": PLU, "tags": PLU},
            ),
        ),
        (L3, ("Test Apple", Category.PRODUCE, (), {"canonical_name": PLU, "category": PLU})),
    ],
    ids=["organic", "conventional"],
)
def test_pc_tc_66_a_plu_hit_fills_what_the_source_supplies(
    plu: PluCatalog,
    raw_text: str,
    expected: tuple[str, Category, tuple[str, ...], dict[str, FieldSource]],
) -> None:
    harness = _harness(plu, coded_answer([raw_text]))

    receipt_id = _process(harness)

    (stored,) = _products(harness, receipt_id)
    name, category, tags, sources = expected
    assert stored == _product(stored.id, name, sources, category=category, tags=tags)
    describes = _events(harness, receipt_id, TOOL, DESCRIBE)
    assert [event.agent for event in describes] == [AgentRole.SUPERVISOR]
    _assert_no_model_after_extraction(harness, receipt_id)


# --- PC-TC-67: a miss changes nothing -------------------------------------------------------------


def test_pc_tc_67_a_miss_leaves_printed_text_products_as_they_are(plu: PluCatalog) -> None:
    harness = _harness(plu, coded_answer([C1, U1], store_name=STORE_NAME))

    receipt_id = _process(harness)

    choc, eggs = _products(harness, receipt_id)
    assert [choc, eggs] == [
        _product(choc.id, "CHOC PISTACH", {"canonical_name": RECEIPT_TEXT}),
        _product(eggs.id, U1, {"canonical_name": RECEIPT_TEXT}),
    ]
    assert _events(harness, receipt_id, TOOL, DESCRIBE) == []
    _assert_no_model_after_extraction(harness, receipt_id)


# --- PC-TC-68: the entry comes from matching; only this receipt's new products ------------------


def test_pc_tc_68_enrichment_uses_the_entry_matching_found(plu: PluCatalog) -> None:
    harness = _harness(plu, coded_answer([L3, C1]))

    receipt_id = _process(harness)

    lookups = _events(harness, receipt_id, TOOL, "catalog_lookup")
    assert [event.agent for event in lookups] == [AgentRole.MATCHING] * 2
    apple, choc = (require_id(product.id) for product in _products(harness, receipt_id))
    saved = harness.checkpointer.get_tuple(
        {"configurable": {"thread_id": thread_id(receipt_id), "checkpoint_ns": ""}}
    )
    assert saved is not None
    assert saved.checkpoint["channel_values"]["created"] == [
        {"position": 0, "product_id": apple, "catalog": TEST_APPLE_ENTRY},
        {"position": 1, "product_id": choc, "catalog": None},
    ]
    assert len(_events(harness, receipt_id, TOOL, DESCRIBE)) == 1


def test_pc_tc_68_the_next_identical_receipt_describes_nothing(plu: PluCatalog) -> None:
    harness = _harness(plu, coded_answer([L3, C1]), coded_answer([L3, C1]))
    first = _process(harness)

    second = _process(harness)

    assert harness.match_sources(second) == [MatchSource.CATALOG] * 2
    assert _products(harness, second) == _products(harness, first)
    assert _events(harness, second, TOOL, DESCRIBE) == []
    assert scripted(harness).calls == 2  # the two extractions


def test_pc_tc_68_two_lines_of_one_new_product_describe_it_once(plu: PluCatalog) -> None:
    harness = _harness(plu, coded_answer([L3, L3]))

    receipt_id = _process(harness)

    first, second = harness.receipt(receipt_id).items
    assert first.product_id == second.product_id
    assert len(_events(harness, receipt_id, TOOL, DESCRIBE)) == 1


# --- PC-TC-69: no model in enrich; the switch -----------------------------------------------------


def test_pc_tc_69_a_queued_reply_is_never_asked_for(plu: PluCatalog) -> None:
    harness = _harness(plu, coded_answer([L3]), coded_answer([L3]))

    receipt_id = _process(harness)

    _assert_no_model_after_extraction(harness, receipt_id)
    assert "commit_enrichment" in harness.node_runs(receipt_id)


def test_with_enrichment_off_the_stage_does_not_run(plu: PluCatalog) -> None:
    harness = _harness(plu, coded_answer([L3]), enrichment_on=False)

    receipt_id = _process(harness)

    assert harness.node_runs(receipt_id)[-1] == "commit_matches"
    (stored,) = _products(harness, receipt_id)
    assert stored == _product(stored.id, "Test Apple", {"canonical_name": PLU})
    _assert_no_model_after_extraction(harness, receipt_id)


# --- PC-TC-70: a person's category correction, entry or not, stage on or off ---------------------


def _remember_category(harness: Harness, raw_text: str, store_key: str) -> None:
    remembered = correction(
        kind=CorrectionKind.CATEGORY,
        subject=normalize_alias(raw_text),
        old_value=None,
        new_value=Category.DAIRY_EGGS.value,
        store_key=store_key,
    )
    in_unit(harness.uow_factory, lambda uow: uow.corrections.add(remembered))


@pytest.mark.parametrize("enrichment_on", [True, False], ids=["enrichment-on", "enrichment-off"])
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ((L3, LOBLAWS, LOBLAWS_KEY), ("Test Apple", PLU)),
        ((C1, STORE_NAME, STORE_KEY), ("CHOC PISTACH", RECEIPT_TEXT)),
    ],
    ids=["plu-entry", "no-source"],
)
def test_pc_tc_70_a_persons_category_correction_reaches_every_new_product(
    plu: PluCatalog,
    line: tuple[str, str, str],
    expected: tuple[str, FieldSource],
    enrichment_on: bool,
) -> None:
    raw_text, store_name, store_key = line
    harness = _harness(
        plu, coded_answer([raw_text], store_name=store_name), enrichment_on=enrichment_on
    )
    _remember_category(harness, raw_text, store_key)

    receipt_id = _process(harness)

    (stored,) = _products(harness, receipt_id)
    name, name_source = expected
    sources = {"canonical_name": name_source, "category": PERSON}
    assert stored == _product(stored.id, name, sources, category=Category.DAIRY_EGGS)
    _assert_no_model_after_extraction(harness, receipt_id)


def test_pc_tc_70_a_correction_at_another_store_is_not_applied(
    plu: PluCatalog,
) -> None:
    harness = _harness(plu, coded_answer([L3]))
    _remember_category(harness, L3, "walmart")

    receipt_id = _process(harness)

    (stored,) = _products(harness, receipt_id)
    assert stored == _product(
        stored.id,
        "Test Apple",
        {"canonical_name": PLU, "category": PLU},
        category=Category.PRODUCE,
    )


# --- the describe payloads (PC-TC-62, 70: rewritten from the save_enrichment payload tests) ------


def test_commit_enrichment_describes_each_product_with_its_line_and_store(
    plu: PluCatalog,
) -> None:
    recorder: list[RecordingToolGateway] = []

    def record(gateway: ToolGateway) -> ToolGateway:
        recorder.append(RecordingToolGateway(gateway))
        return recorder[0]

    harness = _harness(plu, coded_answer([L3, L4]), wrap_tools=record)

    receipt_id = _process(harness)

    apple, organic = (require_id(product.id) for product in _products(harness, receipt_id))
    assert recorder[0].payloads(DESCRIBE) == [
        {
            "product_id": apple,
            "fields": {"name": "Test Apple", "category": "produce"},
            "sources": {"canonical_name": "plu", "category": "plu"},
            "alias": L3,
            "store_key": LOBLAWS_KEY,
        },
        {
            "product_id": organic,
            "fields": {"name": "Organic Test Apple", "category": "produce", "tags": ["organic"]},
            "sources": {"canonical_name": "plu", "category": "plu", "tags": "plu"},
            "alias": L4,
            "store_key": LOBLAWS_KEY,
        },
    ]
    roles = {role for role, name, _ in recorder[0].calls if name == DESCRIBE}
    assert roles == {AgentRole.SUPERVISOR}
    assert harness.node_runs(receipt_id)[-4:] == [
        "match",
        "commit_matches",
        "enrich",
        "commit_enrichment",
    ]
    assert harness.steps(receipt_id)[-3:] == [
        (TOOL, "save_matches"),
        (TOOL, DESCRIBE),
        (TOOL, DESCRIBE),
    ]


def test_describe_from_catalog_called_by_the_enrichment_agent_is_denied(
    plu: PluCatalog,
) -> None:
    harness = _harness(
        plu,
        coded_answer([L3]),
        wrap_tools=lambda gateway: MisroutedToolGateway(gateway, DESCRIBE, AgentRole.ENRICHMENT),
    )

    with capture_logs() as logs:
        outcome = harness.process_image(fresh_label())

    assert outcome.graph is None
    assert outcome.failure is not None
    assert f"denied: role=enrichment tool={DESCRIBE}" in outcome.failure
    denied = [entry for entry in logs if entry["event"] == "tool.denied"]
    assert [(entry["role"], entry["tool"]) for entry in denied] == [("enrichment", DESCRIBE)]
    (stored,) = _products(harness, outcome.receipt_id)
    assert stored.category is None


# --- PC-TC-71: a crash during enrichment, then resume ---------------------------------------------


def test_pc_tc_71_a_crash_during_enrichment_resumes_the_describes_only(plu: PluCatalog) -> None:
    twin = _harness(plu, coded_answer([L3, L4]))
    twin_id = _process(twin)
    expected = _products(twin, twin_id)
    first = _harness(
        plu,
        coded_answer([L3, L4]),
        wrap_tools=lambda gateway: CrashAfterToolGateway(gateway, DESCRIBE),
    )
    crashed = first.process_image(fresh_label())
    assert crashed.failure is not None
    receipt_id = crashed.receipt_id
    assert first.match_sources(receipt_id) == [MatchSource.NEW] * 2
    second = _harness(plu, harness=first)

    resumed = second.resume(receipt_id)

    assert resumed.graph is not None
    assert resumed.graph.resumed_from == "enrich"
    assert scripted(second).calls == 0
    assert second.node_runs(receipt_id) == ["commit_enrichment"]
    assert len(_events(second, receipt_id, TOOL, DESCRIBE)) == 2
    assert _products(second, receipt_id) == expected
    assert second.match_sources(receipt_id) == twin.match_sources(twin_id)
    stored = second.receipt(receipt_id)
    assert (stored.status, stored.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
