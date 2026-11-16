"""Discounts through the receipt graph (discounts-and-paid-prices plan R1, R2, R5, R7, R10, Q32;
TC-89 to TC-94, TC-126; claude-extraction plan E4).

Claude answers each item with its own discount; the mapper stores it as an ITEM line (the
regular price) followed by a DISCOUNT line linked to it, and an order discount as an unlinked
DISCOUNT line. With an empty database every ITEM line becomes a new product.

Pinned:
- the Metro answer is saved with five linked discounts; each linked item's price observation
  has the discount, paid = line total + discount and on_sale; the others discount 0;
- an order discount stays at receipt level: one NOTE named ``discounts`` (SUPERVISOR), reason
  ``as_read`` (R6: a Claude reading placed its discounts, no printed-reference rule runs); its
  wording is ``test_tracing.py``'s (TC-91);
- no model output schema carries the link field (TC-89);
- a crash after ``save_matches`` or ``save_receipt`` resumes with the same links and
  observations (TC-92, TC-93); a person's corrected draft keeps its links (TC-94);
- a negative ITEM line on a receipt a person forced through gets no price observation (Q32).
"""

import json
from collections.abc import Callable
from dataclasses import replace

import pytest

from paper_trail.agents.prompts import PROMPTS_DIR
from paper_trail.agents.schemas import ReceiptAnswer
from paper_trail.domain.enums import AgentRole, LineKind, ReceiptStatus
from paper_trail.domain.models import JsonValue, PriceObservation, Receipt
from paper_trail.domain.trace import TraceEvent, TraceKind
from paper_trail.ports.tools import ToolGateway
from tests.agent_harness import Harness, agent_settings, build_harness
from tests.extractions import (
    C1,
    adjusted_answer,
    adjustment,
    answer,
    inconsistent_answer,
    item,
    metro_answer,
    receipt_answer,
)
from tests.factories import CHOC, KIWI, discount_line, draft_with, item_line
from tests.fakes.reader import Reply, ScriptedReader
from tests.fakes.tools import CrashAfterToolGateway
from tests.support import require_id
from tests.unit.agents.graph_support import graph, second

type Observed = tuple[int, int, bool]  # discount, paid (cents), on sale

METRO_LINKS = {3: 2, 5: 4, 7: 6, 9: 8, 11: 10}
METRO_PAID: dict[int, Observed] = {
    0: (0, 229, False),
    1: (0, 279, False),
    2: (-30, 469, True),
    4: (-161, 80, True),
    6: (-100, 1299, True),
    8: (-50, 549, True),
    10: (-300, 1299, True),
}


def _harness(
    *replies: Reply, wrap_tools: Callable[[ToolGateway], ToolGateway] | None = None
) -> Harness:
    return build_harness(ScriptedReader(*replies), wrap_tools=wrap_tools)


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
            observation.paid.amount_cents,
            observation.on_sale,
        )
        for position, observation in _observations(harness, receipt_id).items()
    }


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


# --- TC-90: the Metro answer, end to end -------------------------------------------------------


def test_tc_90_the_metro_answer_is_saved_with_its_links_and_paid_prices() -> None:
    harness = _harness(metro_answer())

    result = graph(harness.process_image())

    assert result.status is ReceiptStatus.COMPLETED
    assert _links(harness.receipt(result.receipt_id)) == METRO_LINKS
    assert _paid(harness, result.receipt_id) == METRO_PAID
    assert _save_counts(harness, result.receipt_id) == [(5, 0)]
    assert _notes(harness, result.receipt_id) == []


# --- TC-89: the link never reaches a model -----------------------------------------------------


def test_tc_89_no_model_output_schema_carries_the_link() -> None:
    schemas = [json.dumps(ReceiptAnswer.model_json_schema())]
    prompt_schemas = [path.read_text("utf-8") for path in PROMPTS_DIR.rglob("schema.json")]

    assert [schema for schema in schemas + prompt_schemas if "applies_to" in schema] == []


# --- TC-91: the receipt-level NOTE -------------------------------------------------------------


def test_tc_91_an_order_discount_stays_at_receipt_level_with_one_supervisor_note() -> None:
    harness = _harness(adjusted_answer())

    result = graph(harness.process_image())

    (note,) = _notes(harness, result.receipt_id)
    assert (note.agent, note.data) == (
        AgentRole.SUPERVISOR,
        {"receipt_level": [{"position": 4, "raw_text": "MEMBER SAVINGS", "reason": "as_read"}]},
    )
    assert _links(harness.receipt(result.receipt_id)) == {}
    assert _save_counts(harness, result.receipt_id) == [(0, 1)]


def test_r6_an_order_discount_naming_an_items_code_stays_unlinked() -> None:
    """R6: a Claude reading's item discounts are already on their items; the printed-reference
    rule for receipt-level discounts is not applied to it."""
    reading = receipt_answer(
        [item(C1, "16.99", code="1362394")],
        total="12.99",
        adjustments=(adjustment("order_discount", "0000000001 / 1362394", "4.00"),),
    )
    harness = _harness(reading)

    stored = harness.receipt(graph(harness.process_image()).receipt_id)

    assert [
        (line.kind, line.raw_text, line.line_total.amount_cents, line.applies_to_position)
        for line in stored.items
    ] == [
        (LineKind.ITEM, C1, 1699, None),
        (LineKind.DISCOUNT, "0000000001 / 1362394", -400, None),
    ]


# --- TC-92, TC-93: resume after a crash --------------------------------------------------------


def test_tc_92_a_crash_after_save_matches_resumes_with_the_same_observations() -> None:
    first = _harness(
        metro_answer(), wrap_tools=lambda gateway: CrashAfterToolGateway(gateway, "save_matches")
    )
    crashed = first.process_image()
    assert crashed.failure is not None
    before = _observations(first, crashed.receipt_id)

    resumed = graph(second(first).resume(crashed.receipt_id))

    assert resumed.status is ReceiptStatus.COMPLETED
    assert _observations(first, crashed.receipt_id) == before
    assert _paid(first, crashed.receipt_id) == METRO_PAID
    assert _links(first.receipt(crashed.receipt_id)) == METRO_LINKS


def test_tc_93_a_crash_after_save_receipt_saves_again_with_the_same_links() -> None:
    first = _harness(
        metro_answer(), wrap_tools=lambda gateway: CrashAfterToolGateway(gateway, "save_receipt")
    )
    crashed = first.process_image()
    assert crashed.failure is not None
    assert _links(first.receipt(crashed.receipt_id)) == METRO_LINKS
    later = second(first)

    resumed = graph(later.resume(crashed.receipt_id))

    assert resumed.status is ReceiptStatus.COMPLETED
    assert _links(first.receipt(crashed.receipt_id)) == METRO_LINKS
    assert _paid(first, crashed.receipt_id) == METRO_PAID
    assert _save_counts(later, crashed.receipt_id) == [(5, 0)]


# --- TC-94: a person's corrected draft ---------------------------------------------------------


def test_tc_94_a_review_answer_keeps_its_link_and_the_rule_fills_the_rest() -> None:
    harness = build_harness(
        ScriptedReader(inconsistent_answer()), settings=agent_settings(max_attempts=1)
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
    """The line is matched, or linked by the review answer and so left out of matching. A
    negative price paid fails validation (CE-TC-011), so only a person can force it through."""
    reply = answer(subtotal="5.59", taxes=[], total="5.59")
    reply["items"][2] = item("RETURN KS SOURDOUGH", "-9.98", name="Kirkland Sourdough return")
    harness = build_harness(ScriptedReader(reply), settings=agent_settings(max_attempts=1))
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
