"""Discounts through the receipt graph (discounts-and-paid-prices plan R1, R2, R5, R7, R10, Q32;
TC-89 to TC-94).

The Costco photo's printed answer (``costco_printed``: five ``1362394`` items each followed by
``TPD/1362394`` -4.00, two ``1543210`` items with ``TPD/1543210`` -4.00, and a ``MULTIPLE``
-6.00) runs through ``extraction/v2`` on fakes. With an empty database every ITEM line becomes a
new product, so every item is matched and priced with no matching model call.

Pinned:
- ``save_receipt`` links each coded discount to the nearest preceding untaken item with that
  code; ``MULTIPLE`` stays at receipt level. Its output counts 7 linked and 1 receipt-level
  (the row's wording is ``test_tracing.py``'s, TC-88).
- Each linked item's price observation has the discount, paid = line total + discount and
  on_sale; every other one has discount 0, paid = line total, not on sale.
- One NOTE named ``discounts`` (SUPERVISOR) per ``save_receipt`` when a discount is
  receipt-level (Q10, Q29). This is the wiring check; the NOTE's wording, data and the
  no-NOTE cases are ``test_tracing.py``'s (TC-91, TC-126).
- No model output schema carries the link field.
- A negative ITEM line on a receipt a person forced through gets no price observation, the
  receipt completes, and a ``prices`` NOTE names the line (Q32), whether matching linked it or
  the review answer named its product.
"""

import json
from collections.abc import Callable
from dataclasses import replace

import pytest

from paper_trail.agents.prompts import PROMPTS_DIR
from paper_trail.agents.schemas import ExtractionOutput, MatchDecision, PrintedExtraction
from paper_trail.application.process_receipt import ProcessOutcome
from paper_trail.domain.enums import AgentRole, LineKind, ReceiptStatus
from paper_trail.domain.models import JsonValue, PriceObservation, Receipt
from paper_trail.domain.trace import TraceEvent, TraceKind
from paper_trail.ports.agents import GraphOutcome
from paper_trail.ports.tools import ToolGateway
from tests.agent_harness import Harness, agent_settings, build_harness
from tests.extractions import extraction, inconsistent_extraction, printed_fixture
from tests.factories import (
    CHOC,
    KIWI,
    PRINTED_LINKS,
    discount_line,
    draft_with,
    item_line,
    printed_lines,
)
from tests.fakes.llm import Reply, ScriptedLlm
from tests.fakes.tools import CrashAfterToolGateway
from tests.support import require_id

type Observed = tuple[int, int, int, bool]  # discount, unit discount, paid (cents), on sale


def _harness(
    *replies: Reply, wrap_tools: Callable[[ToolGateway], ToolGateway] | None = None
) -> Harness:
    return build_harness(
        ScriptedLlm(*replies),
        settings=agent_settings(extraction_version="v2"),
        wrap_tools=wrap_tools,
    )


def _second(first: Harness) -> Harness:
    """Another process on the same data; no scripted reply, so a model call fails the test."""
    return build_harness(
        ScriptedLlm(),
        settings=first.settings,
        checkpointer=first.checkpointer,
        uow_factory=first.uow_factory,
        images=first.images,
    )


def _graph(outcome: ProcessOutcome) -> GraphOutcome:
    assert outcome.failure is None, outcome.failure
    assert outcome.graph is not None
    return outcome.graph


def _links(receipt: Receipt) -> dict[int, int]:
    return {
        item.position: item.applies_to_position
        for item in receipt.items
        if item.applies_to_position is not None
    }


def _observations(harness: Harness, receipt_id: int) -> dict[int, PriceObservation]:
    """The receipt's price observations by line position."""
    receipt = harness.receipt(receipt_id)
    positions = {require_id(item.id): item.position for item in receipt.items}
    products = {item.product_id for item in receipt.items if item.product_id is not None}
    found: dict[int, PriceObservation] = {}
    for product_id in sorted(products):
        for observation in harness.price_history(product_id):
            if observation.receipt_id == receipt_id:
                position = positions[observation.line_item_id]
                assert position not in found, f"two observations for line {position}"
                found[position] = observation
    return found


def _paid(harness: Harness, receipt_id: int) -> dict[int, Observed]:
    return {
        position: (
            observation.discount.amount_cents,
            observation.unit_discount.amount_cents,
            observation.paid.amount_cents,
            observation.on_sale,
        )
        for position, observation in _observations(harness, receipt_id).items()
    }


def _expected_paid() -> dict[int, Observed]:
    """PRINTED's ITEM lines: -4.00 on each linked item, nothing on the others."""
    linked = set(PRINTED_LINKS.values())
    expected: dict[int, Observed] = {}
    for line in printed_lines():
        if line.kind is LineKind.ITEM:
            discount = -400 if line.position in linked else 0
            total = line.line_total.amount_cents
            expected[line.position] = (discount, discount, total + discount, discount < 0)
    return expected


def _save_counts(harness: Harness, receipt_id: int) -> list[tuple[JsonValue, int]]:
    """(linked, receipt-level) of each ``save_receipt`` call, from its traced output."""
    counts: list[tuple[JsonValue, int]] = []
    for event in harness.events(receipt_id):
        if event.kind is TraceKind.TOOL and event.name == "save_receipt":
            output = event.data["output"]
            assert isinstance(output, dict)
            receipt_level = output["discounts_receipt_level"]
            assert isinstance(receipt_level, list)
            counts.append((output["discounts_linked"], len(receipt_level)))
    return counts


def _notes(harness: Harness, receipt_id: int, name: str = "discounts") -> list[TraceEvent]:
    return [
        event
        for event in harness.events(receipt_id)
        if event.kind is TraceKind.NOTE and event.name == name
    ]


# --- TC-90: the Costco answer, end to end ------------------------------------------------------


def test_tc_90_the_costco_answer_is_saved_with_its_links_and_paid_prices() -> None:
    harness = _harness(printed_fixture("costco_printed"))

    graph = _graph(harness.process_image())

    assert graph.status is ReceiptStatus.COMPLETED
    assert _links(harness.receipt(graph.receipt_id)) == PRINTED_LINKS
    assert _paid(harness, graph.receipt_id) == _expected_paid()
    assert _save_counts(harness, graph.receipt_id) == [(7, 1)]


# --- TC-89: the link never reaches a model -----------------------------------------------------


def test_tc_89_no_model_output_schema_carries_the_link() -> None:
    schemas = [
        json.dumps(model.model_json_schema())
        for model in (ExtractionOutput, PrintedExtraction, MatchDecision)
    ]
    prompt_schemas = [path.read_text("utf-8") for path in PROMPTS_DIR.rglob("schema.json")]

    assert prompt_schemas
    assert [schema for schema in schemas + prompt_schemas if "applies_to" in schema] == []


# --- TC-91: the receipt-level NOTE -------------------------------------------------------------


def test_tc_91_the_receipt_level_discounts_share_one_supervisor_note() -> None:
    answer = printed_fixture("costco_printed")
    answer["items"][6]["raw_text"] = "INSTANT SAVINGS"  # no code: NO_REFERENCE
    harness = _harness(answer)

    graph = _graph(harness.process_image())

    (note,) = _notes(harness, graph.receipt_id)
    assert (note.agent, note.data) == (
        AgentRole.SUPERVISOR,
        {
            "receipt_level": [
                {"position": 4, "raw_text": "MULTIPLE", "reason": "multiple"},
                {"position": 6, "raw_text": "INSTANT SAVINGS", "reason": "no_reference"},
            ]
        },
    )
    assert _links(harness.receipt(graph.receipt_id)) == {
        link: item for link, item in PRINTED_LINKS.items() if link != 6
    }


# --- TC-92, TC-93: resume after a crash --------------------------------------------------------


def test_tc_92_a_crash_after_save_matches_resumes_with_the_same_observations() -> None:
    first = _harness(
        printed_fixture("costco_printed"),
        wrap_tools=lambda gateway: CrashAfterToolGateway(gateway, "save_matches"),
    )
    crashed = first.process_image()
    assert crashed.failure is not None
    before = _observations(first, crashed.receipt_id)

    resumed = _graph(_second(first).resume(crashed.receipt_id))

    assert resumed.status is ReceiptStatus.COMPLETED
    assert _observations(first, crashed.receipt_id) == before
    assert _paid(first, crashed.receipt_id) == _expected_paid()
    assert _links(first.receipt(crashed.receipt_id)) == PRINTED_LINKS


def test_tc_93_a_crash_after_save_receipt_saves_again_with_the_same_links() -> None:
    first = _harness(
        printed_fixture("costco_printed"),
        wrap_tools=lambda gateway: CrashAfterToolGateway(gateway, "save_receipt"),
    )
    crashed = first.process_image()
    assert crashed.failure is not None
    assert _links(first.receipt(crashed.receipt_id)) == PRINTED_LINKS
    second = _second(first)

    resumed = _graph(second.resume(crashed.receipt_id))

    assert resumed.status is ReceiptStatus.COMPLETED
    assert _links(first.receipt(crashed.receipt_id)) == PRINTED_LINKS
    assert _paid(first, crashed.receipt_id) == _expected_paid()
    assert _save_counts(second, crashed.receipt_id) == [(7, 1)]


# --- TC-94: a person's corrected draft ---------------------------------------------------------


def test_tc_94_a_review_answer_keeps_its_link_and_the_rule_fills_the_rest() -> None:
    harness = build_harness(
        ScriptedLlm(inconsistent_extraction()), settings=agent_settings(max_text_attempts=1)
    )
    parked = harness.process_image()
    assert parked.graph is not None
    assert parked.graph.status is ReceiptStatus.NEEDS_REVIEW
    answer = draft_with(
        [
            item_line(0, CHOC, 1699),
            item_line(1, CHOC, 1699),
            discount_line(2, "TPD/1362394", -400, applies_to=0),
            item_line(3, KIWI, 1599),
            discount_line(4, "TPD/1543210", -400),
        ],
        subtotal=4197,
        tax=0,
        total=4197,
    )

    harness.resolve(require_id(parked.graph.review_id), answer)

    stored = harness.receipt(parked.receipt_id)
    assert stored.status is ReceiptStatus.COMPLETED
    assert _links(stored) == {2: 0, 4: 3}
    assert _save_counts(harness, parked.receipt_id) == [(2, 0)]


# --- TC-126 (Q32): a negative item line on a forced receipt ------------------------------------


@pytest.mark.parametrize("named", [None, "Kirkland Sourdough"], ids=["matched", "review_named"])
def test_tc_126_a_forced_receipt_with_a_negative_item_line_completes_without_its_price(
    named: str | None,
) -> None:
    """The line is matched, or linked by the review answer and so left out of matching."""
    answer = extraction(subtotal_cents=559, tax_cents=0, total_cents=559)
    answer["items"][2] = {
        "raw_text": "RETURN KS SOURDOUGH",
        "description": "Kirkland Sourdough Bread return",
        "quantity": "1",
        "unit_price_cents": None,
        "line_total_cents": -998,
        "kind": "item",
    }
    harness = build_harness(ScriptedLlm(answer), settings=agent_settings(max_text_attempts=1))
    parked = harness.process_image()
    assert parked.graph is not None
    assert parked.graph.status is ReceiptStatus.NEEDS_REVIEW

    draft = harness.runner.pending_draft(parked.receipt_id)
    assert draft is not None
    if named is not None:
        draft = replace(draft, product_names={2: named})

    resolved = harness.resolve(
        require_id(parked.graph.review_id), draft, force=True, reason="a return, checked on paper"
    )

    assert resolved.status is ReceiptStatus.COMPLETED
    assert sorted(_observations(harness, parked.receipt_id)) == [0, 1]
    (note,) = _notes(harness, parked.receipt_id, "prices")
    assert (note.agent, note.data) == (
        AgentRole.SUPERVISOR,
        {"unpriced": [{"position": 2, "raw_text": "RETURN KS SOURDOUGH"}]},
    )
