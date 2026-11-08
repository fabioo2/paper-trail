"""STRETCH S1 (cut together with this file): a multi-buy whose one printed amount covers
several products (EA-AC-12; docs/plans/extraction-accuracy.md 2.2).

The Superstore photo printed ``2 @ 2/$10.00  10.00`` under two different products. The
printed-value answer gives the first product ``amount: null`` and the second the shared amount
with quantity 2. ``PrintedExtraction.to_output()`` (through ``share_amounts`` in
``paper_trail.domain.rules.printed``) splits it:

- a run of k >= 1 lines with no amount, followed by a priced line whose quantity is k+1,
  becomes k+1 lines of quantity 1 and no unit price, the amount split evenly (remainder cents
  on the last line);
- any other missing amount is a ``PrintedValueError`` saying "no amount printed" for that line
  (the guard stops a model from hiding an unreadable price).

Tested through ``to_output()`` rather than ``share_amounts`` itself: the plan does not fix the
domain-side line type ``share_amounts`` takes, and the behaviour is what EA-AC-12 asks for.
If S1 is cut, ``extraction/v2`` example 01 must lose its shared amount (``test_prompts_v2.py``).
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from paper_trail.agents.schemas import ExtractionOutput, PrintedExtraction
from paper_trail.domain.enums import ReceiptStatus
from paper_trail.domain.errors import PrintedValueError
from paper_trail.domain.rules.arithmetic import check_arithmetic
from tests.agent_harness import agent_settings, build_harness
from tests.extractions import printed_fixture
from tests.fakes.clock import FakeClock
from tests.fakes.llm import ScriptedLlm

EA_NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
NO_AMOUNT = "no amount printed"


def _line(
    raw_text: str, amount: str | None, quantity: str | None = None, unit: str | None = None
) -> dict[str, Any]:
    return {
        "raw_text": raw_text,
        "description": raw_text.title(),
        "quantity": quantity,
        "unit_price": unit,
        "amount": amount,
        "kind": "item",
    }


def _receipt(lines: list[dict[str, Any]], total: str) -> dict[str, Any]:
    return {
        "store_name": "SUPERSTORE #9999",
        "purchased_on": "26/11/02",
        "currency": "CAD",
        "subtotal": None,
        "taxes": [],
        "total": total,
        "payment_last4": None,
        "items": lines,
        "confidence": 0.9,
    }


def _convert(payload: dict[str, Any]) -> ExtractionOutput:
    return PrintedExtraction.model_validate_json(json.dumps(payload)).to_output()


def test_ea_ac_12_one_amount_over_two_products_is_split() -> None:
    output = _convert(
        _receipt(
            [
                _line("PC BLACK BEANS", None),
                _line("PC CHICKPEAS", "10.00", quantity="2", unit="2/$10.00"),
            ],
            total="10.00",
        )
    )

    assert [line.raw_text for line in output.items] == ["PC BLACK BEANS", "PC CHICKPEAS"]
    assert [line.line_total_cents for line in output.items] == [500, 500]
    assert [Decimal(line.quantity) for line in output.items] == [1, 1]
    assert [line.unit_price_cents for line in output.items] == [None, None]


def test_ea_ac_12_an_odd_split_puts_the_remainder_on_the_last_line() -> None:
    output = _convert(
        _receipt(
            [_line("A", None), _line("B", "10.01", quantity="2")],
            total="10.01",
        )
    )

    assert [line.line_total_cents for line in output.items] == [500, 501]


def test_ea_ac_12_a_run_of_three_products() -> None:
    output = _convert(
        _receipt(
            [_line("A", None), _line("B", None), _line("C", "15.00", quantity="3")],
            total="15.00",
        )
    )

    assert [line.line_total_cents for line in output.items] == [500, 500, 500]


@pytest.mark.parametrize(
    "lines",
    [
        [_line("A", None), _line("B", "10.00")],  # the priced line's quantity is 1, not 2
        [_line("A", None), _line("B", "15.00", quantity="3")],  # one missing amount, not two
        [_line("A", "10.00", quantity="2"), _line("B", None)],  # nothing priced after it
        [_line("A", None)],
    ],
    ids=["quantity-1", "quantity-too-big", "last-line", "alone"],
)
def test_ea_ac_12_any_other_missing_amount_is_an_error(lines: list[dict[str, Any]]) -> None:
    with pytest.raises(PrintedValueError) as raised:
        _convert(_receipt(lines, total="10.00"))

    assert NO_AMOUNT in str(raised.value)


def test_ea_ac_12_the_superstore_answer_converts_and_adds_up() -> None:
    output = _convert(printed_fixture("superstore_printed"))

    assert len(output.items) == 6
    draft = output.to_draft(today=EA_NOW.date(), ocr_text=None, image_path=None)
    report = check_arithmetic(draft)
    assert report.ok, report.issues
    assert draft.purchased_on == date(2026, 11, 2)


def test_ea_ac_12_the_superstore_answer_completes_end_to_end() -> None:
    """The plan's stretch success criterion, on fakes: COMPLETED, dated 2026-11-02."""
    harness = build_harness(
        ScriptedLlm(printed_fixture("superstore_printed")),
        settings=agent_settings(extraction_version="v2"),
        clock=FakeClock(EA_NOW),
    )

    outcome = harness.process_image("superstore-2026-11-02")

    assert outcome.graph is not None
    assert outcome.graph.status is ReceiptStatus.COMPLETED
    receipt = harness.receipt(outcome.receipt_id)
    assert receipt.purchased_on == date(2026, 11, 2)
    assert len(receipt.items) == 6
