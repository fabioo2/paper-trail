"""Enrichment through the whole graph (PLAN 6, agent core slice D): AC-D-02 = S4-AC-05 at graph
level, S4-AC-04 in full ("creates a new product, enriches it, stores its vector, and the next
identical line hits the alias"), and S3-AC-10 for a GPU drop during enrichment.

Scripted LLM (extraction, then one enrichment answer per new product, in line order), fake
embeddings, in-process tools; the MCP transport is compared in ``test_transport_equivalence.py``.

Pinned (docs/tests/agent-core-D.md):
- ``GRAPH__ENRICHMENT`` (``GraphSettings.enrichment``, default true) turns the stage on. Graph:
  ``commit_matches -> enrich -> commit_enrichment -> END`` when it is on and this run's
  ``save_matches`` linked a line with source ``new``; otherwise ``commit_matches -> END``.
  ``enrich`` is the ENRICHMENT agent and only reads; ``commit_enrichment`` is the SUPERVISOR.
- Only the products created by this receipt's matching are enriched: the distinct product ids
  of its ``new`` lines, in line order; the first such line is the product's source line.
  Products found by memory, alias, vector or confirmation, and products a person named in a
  review, are not.
- ``enrich`` makes one request per product: ``settings.ollama.text_model``, temperature 0, tag
  ``enrichment/v1`` (``+noex`` without examples); the final user message holds the source line's
  raw text and description inside ``<receipt_data>``. An answer that never validates (an
  invented category included, S4-AC-05) or whose confidence is below ``GRAPH__MIN_CONFIDENCE``
  leaves that product as it is: no ``save_enrichment`` for it, not retried, the run completes.
- ``commit_enrichment`` calls ``save_enrichment`` once per enriched product, in the same order,
  with ``alias`` the source line's raw text and ``store_key`` the receipt's store key. Every
  enrichment request comes before the first ``save_enrichment`` (one model loaded at a time:
  the text model's calls, then the embedding model's).
- The receipt is COMPLETED/DONE once ``save_matches`` has run; enrichment changes products, not
  the receipt. A GPU drop during enrichment raises ``ProcessingStoppedError``, keeps the
  receipt COMPLETED/DONE and the checkpoint; ``--resume`` continues after ``commit_matches``.
- Trace: enrichment LLM events have agent ENRICHMENT and a summary naming the category;
  ``save_enrichment`` TOOL events have agent SUPERVISOR and a summary naming the stored category,
  with ``memory`` when the correction memory chose it.
"""

from typing import Any

import pytest
from structlog.testing import capture_logs

from paper_trail.agents.graph import NODE_NAMES
from paper_trail.application.process_receipt import ProcessingStoppedError, ProcessOutcome
from paper_trail.domain.enums import (
    AgentRole,
    Category,
    CorrectionKind,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
    Unit,
)
from paper_trail.domain.models import Product
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.domain.trace import TraceEvent, TraceKind
from paper_trail.ports.llm import LlmUnavailableError
from paper_trail.ports.tools import ToolGateway
from paper_trail.ports.unit_of_work import UnitOfWork
from tests.agent_harness import (
    TEXT_MODEL,
    Harness,
    agent_settings,
    build_harness,
)
from tests.extractions import (
    COSTCO_ENRICHMENTS,
    COSTCO_OCR,
    EGGS_ALIAS,
    STORE_KEY,
    STORE_NAME,
    enrichment,
    extraction,
    one_line_extraction,
)
from tests.factories import correction, product
from tests.fakes.embeddings import FakeEmbeddings, basis
from tests.fakes.llm import Reply, ScriptedLlm
from tests.fakes.tools import MisroutedToolGateway, RecordingToolGateway
from tests.storage import in_unit
from tests.support import require_id

LLM = TraceKind.LLM
TOOL = TraceKind.TOOL
ENRICHMENT_TAG = "enrichment/v1"
GPU_DOWN = "cannot reach the LLM host llm-host.test:11434"
RAW_TEXTS = ("KS ORG EGGS 24CT", "KS 2% MILK 4L", "KS SOURDOUGH")
DESCRIPTIONS = ("Kirkland Organic Eggs 24 ct", "Kirkland 2% Milk 4 L", "Kirkland Sourdough Bread")


def _harness(
    *replies: Reply,
    enrichment_on: bool = True,
    examples: bool = True,
    embeddings: FakeEmbeddings | None = None,
) -> Harness:
    return build_harness(
        ScriptedLlm(*replies),
        settings=agent_settings(enrichment=enrichment_on, examples=examples),
        embeddings=embeddings,
    )


def _completed(outcome: ProcessOutcome) -> int:
    assert outcome.failure is None, outcome.failure
    assert outcome.graph is not None
    assert outcome.graph.status is ReceiptStatus.COMPLETED
    return outcome.receipt_id


def _costco(harness: Harness, name: str = "costco.txt") -> int:
    return _completed(harness.process_text(COSTCO_OCR, name))


def _products(harness: Harness, receipt_id: int) -> list[Product]:
    return [
        harness.product(require_id(item.product_id)) for item in harness.receipt(receipt_id).items
    ]


def _enrichment_requests(harness: Harness) -> list[int]:
    return [i for i, request in enumerate(harness.llm.requests) if request.tag == ENRICHMENT_TAG]


def _events(harness: Harness, receipt_id: int, kind: TraceKind, name: str) -> list[TraceEvent]:
    return [
        event for event in harness.events(receipt_id) if event.kind is kind and event.name == name
    ]


def _saved_ids(harness: Harness, receipt_id: int) -> list[int]:
    """The product id of every ``save_enrichment`` output, in call order."""
    ids: list[int] = []
    for event in _events(harness, receipt_id, TOOL, "save_enrichment"):
        output = event.data["output"]
        assert isinstance(output, dict)
        saved = output["product"]
        assert isinstance(saved, dict)
        product_id = saved["id"]
        assert isinstance(product_id, int)
        ids.append(product_id)
    return ids


def _enriched_text(answer: dict[str, Any]) -> str:
    return normalize_alias(" ".join([answer["canonical_name"], *answer["tags"]]))


def _nearest(harness: Harness, vector: tuple[float, ...]) -> tuple[int, float]:
    with harness.uow_factory(read_only=True) as uow:
        (hit,) = uow.vectors.nearest(vector, k=1)
    return hit.product_id, round(hit.distance, 6)


def test_enrich_and_commit_enrichment_are_nodes_of_the_graph() -> None:
    assert NODE_NAMES[-3:] == ("commit_matches", "enrich", "commit_enrichment")


# --- S4-AC-04 in full ---------------------------------------------------------------------------


def test_s4_ac_04_new_products_are_enriched_with_their_vectors() -> None:
    harness = _harness(extraction(), *COSTCO_ENRICHMENTS)

    receipt_id = _costco(harness)

    eggs, milk, bread = _products(harness, receipt_id)
    assert (eggs.canonical_name, eggs.brand, str(eggs.size_value), eggs.size_unit) == (
        "Kirkland Signature Organic Eggs 24 ct",
        "Kirkland Signature",
        "24",
        Unit.EACH,
    )
    assert (eggs.category, eggs.tags) == (Category.DAIRY_EGGS, ("eggs", "organic", "breakfast"))
    assert (milk.category, milk.size_unit) == (Category.DAIRY_EGGS, Unit.L)
    assert (bread.category, bread.size_value, bread.size_unit) == (Category.BAKERY, None, None)
    for stored, answer in zip((eggs, milk, bread), COSTCO_ENRICHMENTS, strict=True):
        vector = harness.embeddings.vector(_enriched_text(answer))
        assert _nearest(harness, vector) == (require_id(stored.id), 0.0)
    assert harness.match_sources(receipt_id) == [MatchSource.NEW] * 3


def test_s4_ac_04_the_next_identical_receipt_hits_the_alias_and_enriches_nothing() -> None:
    harness = _harness(extraction(), *COSTCO_ENRICHMENTS, extraction())
    first = _costco(harness, "a.txt")
    embeddings_before = harness.embeddings.calls
    llm_before = harness.llm.calls

    second = _costco(harness, "b.txt")

    assert harness.match_sources(second) == [MatchSource.ALIAS] * 3
    assert _products(harness, second) == _products(harness, first)
    assert harness.embeddings.calls == embeddings_before
    assert harness.llm.calls == llm_before + 1  # the extraction only
    assert "enrich" not in harness.node_runs(second)
    assert harness.node_runs(second)[-1] == "commit_matches"


def test_enrichment_runs_after_matching_and_the_saves_come_after_every_request() -> None:
    harness = _harness(extraction(), *COSTCO_ENRICHMENTS)

    receipt_id = _costco(harness)

    runs = harness.node_runs(receipt_id)
    assert runs[-4:] == ["match", "commit_matches", "enrich", "commit_enrichment"]
    steps = harness.steps(receipt_id)
    enrich_rows = [(LLM, ENRICHMENT_TAG)] * 3 + [(TOOL, "save_enrichment")] * 3
    assert steps[-6:] == enrich_rows
    assert steps[-7] == (TOOL, "save_matches")


def test_the_enrichment_requests_name_each_new_products_line() -> None:
    harness = _harness(extraction(), *COSTCO_ENRICHMENTS)

    _costco(harness)

    indexes = _enrichment_requests(harness)
    assert len(indexes) == 3
    for index, raw_text, description in zip(indexes, RAW_TEXTS, DESCRIPTIONS, strict=True):
        request = harness.llm.requests[index]
        assert (request.model, request.temperature) == (TEXT_MODEL, 0.0)
        final = harness.llm.last_user_message(index)
        data = final[final.index("<receipt_data>") : final.index("</receipt_data>")]
        assert raw_text in data
        assert description in data


def test_without_examples_the_tag_says_so() -> None:
    harness = _harness(extraction(), *COSTCO_ENRICHMENTS, examples=False)

    _costco(harness)

    assert {harness.llm.requests[i].tag for i in range(1, 4)} == {"enrichment/v1+noex"}


def test_commit_enrichment_saves_each_product_with_its_line_alias_and_store() -> None:
    harness = _harness(extraction(), *COSTCO_ENRICHMENTS)

    receipt_id = _costco(harness)

    saves = _events(harness, receipt_id, TOOL, "save_enrichment")
    assert [event.agent for event in saves] == [AgentRole.SUPERVISOR] * 3
    ids = [require_id(product.id) for product in _products(harness, receipt_id)]
    assert _saved_ids(harness, receipt_id) == ids
    assert [event.summary.count("dairy_eggs") > 0 for event in saves] == [True, True, False]
    assert "bakery" in saves[2].summary
    assert all("memory" not in event.summary for event in saves)


def test_the_enrichment_llm_events_belong_to_the_enrichment_agent() -> None:
    harness = _harness(extraction(), *COSTCO_ENRICHMENTS)

    receipt_id = _costco(harness)

    events = _events(harness, receipt_id, LLM, ENRICHMENT_TAG)
    assert [event.agent for event in events] == [AgentRole.ENRICHMENT] * 3
    assert "dairy_eggs" in events[0].summary
    assert "bakery" in events[2].summary


# --- only the products this receipt created -------------------------------------------------------


def test_two_lines_of_one_new_product_enrich_it_once() -> None:
    eggs_twice: dict[str, Any] = one_line_extraction("KS ORG EGGS 24CT", cents=1798)
    line = dict(eggs_twice["items"][0], line_total_cents=899, unit_price_cents=899)
    eggs_twice["items"] = [line, dict(line)]
    harness = _harness(eggs_twice, enrichment())

    receipt_id = _completed(harness.process_text("KS ORG EGGS 24CT 8.99\nKS ORG EGGS 24CT 8.99"))

    first, second = harness.receipt(receipt_id).items
    assert first.product_id == second.product_id
    assert len(_enrichment_requests(harness)) == 1
    assert len(_events(harness, receipt_id, TOOL, "save_enrichment")) == 1


def test_a_product_found_by_the_vector_index_is_not_enriched() -> None:
    harness = _harness(
        one_line_extraction("KS ORG EGGS 24CT"),
        embeddings=FakeEmbeddings({EGGS_ALIAS: basis(0)}),
    )

    def seed(uow: UnitOfWork) -> int:
        product_id = require_id(uow.products.add(product(canonical_name="Organic Eggs")).id)
        uow.vectors.upsert(product_id, basis(0))
        return product_id

    known = in_unit(harness.uow_factory, seed)

    receipt_id = _completed(harness.process_text("KS ORG EGGS 24CT 8.99"))

    assert harness.line(receipt_id, 0).product_id == known
    assert _enrichment_requests(harness) == []
    assert harness.node_runs(receipt_id)[-1] == "commit_matches"


def test_with_enrichment_off_the_stage_does_not_run() -> None:
    harness = _harness(extraction(), enrichment_on=False)

    receipt_id = _costco(harness)

    assert _enrichment_requests(harness) == []
    assert harness.node_runs(receipt_id)[-1] == "commit_matches"
    assert all(product.category is None for product in _products(harness, receipt_id))


# --- S4-AC-05 at graph level ----------------------------------------------------------------------


def test_s4_ac_05_an_invented_category_leaves_the_product_unenriched() -> None:
    harness = _harness(extraction(), enrichment(category="groceries"), *COSTCO_ENRICHMENTS[1:])

    receipt_id = _costco(harness)

    eggs, milk, _ = _products(harness, receipt_id)
    assert (eggs.canonical_name, eggs.category, eggs.tags) == (DESCRIPTIONS[0], None, ())
    assert milk.category is Category.DAIRY_EGGS
    assert require_id(eggs.id) not in _saved_ids(harness, receipt_id)
    assert harness.llm.calls == 4  # not retried


def test_a_low_confidence_answer_leaves_the_product_unenriched() -> None:
    harness = _harness(extraction(), enrichment(confidence=0.3), *COSTCO_ENRICHMENTS[1:])

    receipt_id = _costco(harness)

    eggs, milk, _ = _products(harness, receipt_id)
    assert eggs.category is None
    assert milk.category is Category.DAIRY_EGGS
    assert len(_events(harness, receipt_id, TOOL, "save_enrichment")) == 2


def test_s4_ac_05_a_category_correction_for_the_alias_overrides_the_model() -> None:
    harness = _harness(extraction(), *COSTCO_ENRICHMENTS)
    remembered = correction(
        kind=CorrectionKind.CATEGORY,
        subject=EGGS_ALIAS,
        old_value=None,
        new_value=Category.PANTRY.value,
        store_key=STORE_KEY,
    )
    in_unit(harness.uow_factory, lambda uow: uow.corrections.add(remembered))

    receipt_id = _costco(harness)

    eggs, milk, _ = _products(harness, receipt_id)
    assert eggs.category is Category.PANTRY
    assert eggs.canonical_name == COSTCO_ENRICHMENTS[0]["canonical_name"]
    assert milk.category is Category.DAIRY_EGGS
    first_save = _events(harness, receipt_id, TOOL, "save_enrichment")[0]
    assert "pantry" in first_save.summary
    assert "memory" in first_save.summary


# --- failures -------------------------------------------------------------------------------------


def test_a_gpu_drop_during_enrichment_keeps_the_receipt_and_resumes_enrichment_only() -> None:
    first = _harness(extraction(), COSTCO_ENRICHMENTS[0], LlmUnavailableError(GPU_DOWN))
    with pytest.raises(ProcessingStoppedError) as stopped:
        first.process_text(COSTCO_OCR)
    receipt_id = stopped.value.receipt_id
    saved = first.receipt(receipt_id)
    assert (saved.status, saved.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert first.match_sources(receipt_id) == [MatchSource.NEW] * 3
    assert all(product.category is None for product in _products(first, receipt_id))
    second = build_harness(
        ScriptedLlm(*COSTCO_ENRICHMENTS),
        settings=first.settings,
        checkpointer=first.checkpointer,
        uow_factory=first.uow_factory,
        images=first.images,
    )

    resumed = second.resume(receipt_id)

    assert resumed.graph is not None
    assert resumed.graph.resumed_from == "commit_matches"
    assert resumed.graph.status is ReceiptStatus.COMPLETED
    assert second.llm.calls == 3
    assert {request.tag for request in second.llm.requests} == {ENRICHMENT_TAG}
    categories = [product.category for product in _products(second, receipt_id)]
    assert categories == [Category.DAIRY_EGGS, Category.DAIRY_EGGS, Category.BAKERY]


def test_s5_ac_05_save_enrichment_called_by_the_enrichment_agent_is_denied() -> None:
    harness = build_harness(
        ScriptedLlm(extraction(), *COSTCO_ENRICHMENTS),
        settings=agent_settings(enrichment=True),
        wrap_tools=lambda gateway: MisroutedToolGateway(
            gateway, "save_enrichment", AgentRole.ENRICHMENT
        ),
    )

    with capture_logs() as logs:
        outcome = harness.process_text(COSTCO_OCR)

    assert outcome.graph is None
    assert outcome.failure is not None
    assert "denied: role=enrichment tool=save_enrichment" in outcome.failure
    denied = [entry for entry in logs if entry["event"] == "tool.denied"]
    assert [(entry["role"], entry["tool"]) for entry in denied] == [
        ("enrichment", "save_enrichment")
    ]
    assert all(product.category is None for product in _products(harness, outcome.receipt_id))


def test_commit_enrichment_sends_the_source_lines_alias_and_the_store_key() -> None:
    recorder: list[RecordingToolGateway] = []

    def record(gateway: ToolGateway) -> ToolGateway:
        recorder.append(RecordingToolGateway(gateway))
        return recorder[0]

    harness = build_harness(
        ScriptedLlm(extraction(), *COSTCO_ENRICHMENTS),
        settings=agent_settings(enrichment=True),
        wrap_tools=record,
    )

    receipt_id = _costco(harness)

    payloads = recorder[0].payloads("save_enrichment")
    ids = [require_id(product.id) for product in _products(harness, receipt_id)]
    assert [payload["product_id"] for payload in payloads] == ids
    assert [normalize_alias(str(payload["alias"])) for payload in payloads] == [
        normalize_alias(raw_text) for raw_text in RAW_TEXTS
    ]
    assert {payload["store_key"] for payload in payloads} == {STORE_KEY}
    assert harness.receipt(receipt_id).store_name == STORE_NAME
    roles = {role for role, name, _ in recorder[0].calls if name == "save_enrichment"}
    assert roles == {AgentRole.SUPERVISOR}
