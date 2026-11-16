"""Claude's answer schema and its conversion to cents (claude-extraction plan E1, ADR-0017), and
the ``check_arithmetic`` tool's receipt payload.

Pinned (test-cases.md "Extraction schema", CE-TC-003…005, 008; rulings CQ8, CQ20, I2):
- ``ReceiptAnswer`` forbids unknown fields at every level; every money value is a JSON
  string, never a number; ``code``, ``department``, ``quantity``, ``unit_price``, ``discount``,
  ``subtotal``, ``purchased_on`` and ``payment_last4`` may be null; ``unit`` is exactly
  ``each``, ``kg`` or ``lb``; an adjustment's ``kind`` is ``deposit``, ``fee`` or
  ``order_discount``.
- ``to_reading`` converts in code: a null quantity is 1, a null discount zero; a discount and
  an order discount count by their size whatever sign is printed (an order discount reduces
  the receipt), a deposit or fee as printed. Every unreadable value is listed in one
  ``PrintedValueError`` naming the item and field and quoting the text.
- ``ExtractedReceipt.to_draft`` (the tool payload) maps every field and parses the date.
"""

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from paper_trail.agents.schemas import ReceiptAnswer
from paper_trail.application.payloads import ExtractedReceipt
from paper_trail.domain.enums import Unit
from paper_trail.domain.errors import PrintedValueError
from paper_trail.domain.money import Money
from paper_trail.domain.rules.arithmetic import IssueCode, check_arithmetic
from paper_trail.domain.rules.readings import (
    AdjustmentKind,
    ReadAdjustment,
    ReadItem,
    ReceiptReading,
    check_reading,
)
from tests.extractions import (
    PURCHASED_ON,
    STORE_NAME,
    TODAY,
    adjusted_answer,
    adjustment,
    answer,
    extraction,
    item,
    receipt_answer,
    without_confidence,
)
from tests.factories import cad


def _validate(payload: dict[str, Any]) -> ReceiptAnswer:
    """Validate as the reader does: from the model's JSON text."""
    return ReceiptAnswer.model_validate_json(json.dumps(payload))


def _with_item(**changes: Any) -> dict[str, Any]:
    payload = answer()
    payload["items"][0].update(changes)
    return payload


def _without(payload: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in payload.items() if name != key}


def _one_item(price_paid: str = "8.99", **fields: Any) -> dict[str, Any]:
    return receipt_answer([item("KS ORG EGGS 24CT", price_paid, **fields)], total=price_paid)


# --- CE-TC-003: the schema accepts a complete answer ---------------------------------------------

COMPLETE = receipt_answer(
    [
        item("KS ORG EGGS 24CT", "8.99"),
        item(
            "4011 BANANAS",
            "1.52",
            code="4011",
            name="Bananas",
            department="PRODUCE",
            quantity="0.460",
            unit="kg",
            unit_price="3.31",
        ),
        item(
            "4011 BANANAS LB",
            "0.70",
            code="4011",
            name="Bananas",
            department="PRODUCE",
            quantity="1.01",
            unit="lb",
            unit_price="0.69",
        ),
        item(
            "1362394 CHOC PISTACH",
            "13.99",
            regular_price="17.99",
            discount="4.00",
            code="1362394",
            name="Chocolate pistachio",
            department="GROCERY",
            quantity="1",
            unit="each",
            unit_price="17.99",
        ),
    ],
    subtotal="25.55",
    taxes=("0.70",),
    total="26.25",
    adjustments=(
        adjustment("deposit", "BOTTLE DEPOSIT", "0.10"),
        adjustment("fee", "ECO FEE", "0.25"),
        adjustment("order_discount", "MEMBER SAVINGS", "2.00"),
    ),
    payment_last4="1111",
)


@pytest.mark.parametrize(
    "payload",
    [
        COMPLETE,
        {**COMPLETE, "subtotal": None},
        receipt_answer([item("KS ORG EGGS 24CT", "8.99")], total="8.99"),
    ],
    ids=["complete", "no-subtotal", "no-taxes-no-adjustments"],
)
def test_ce_tc_003_a_complete_answer_parses_as_given(payload: dict[str, Any]) -> None:
    assert _validate(payload).model_dump(mode="json") == payload


# --- CE-TC-004: the schema refuses what the contract forbids --------------------------------------


def _with_adjustment(**changes: Any) -> dict[str, Any]:
    payload = adjusted_answer()
    payload["adjustments"][0].update(changes)
    return payload


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        (_with_item(price_paid=4.69), "price_paid"),
        (answer(total=42.04), "total"),
        (_with_adjustment(amount=0.10), "amount"),
        (answer(total=None), "total"),
        (_without(answer(), "store_name"), "store_name"),
        ({**answer(), "items": [_without(item("EGGS", "8.99"), "price_paid")]}, "price_paid"),
        (_with_item(regular_price=None), "regular_price"),
        (_with_item(unit="g"), "unit"),
        (_with_adjustment(kind="coupon"), "kind"),
        (answer(payment_last4="12345"), "payment_last4"),
        (answer(confidence=0.92), "confidence"),
        (_with_item(kind="item"), "kind"),
        ({**answer(), "lines": []}, "lines"),
        (_without(answer(), "items"), "items"),
        (answer(adjustments=None), "adjustments"),
        (answer(taxes=None), "taxes"),
    ],
    ids=[
        "paid-number",
        "total-number",
        "adjustment-number",
        "total-null",
        "store-missing",
        "paid-missing",
        "regular-null",
        "unit-g",
        "kind-coupon",
        "last4-five-digits",
        "confidence",
        "item-kind",
        "lines",
        "items-missing",
        "adjustments-null",
        "taxes-null",
    ],
)
def test_ce_tc_004_the_schema_refuses_what_the_contract_forbids(
    payload: dict[str, Any], field: str
) -> None:
    with pytest.raises(ValidationError, match=field):
        _validate(payload)


def test_ce_tc_004_the_schema_sent_to_claude_types_money_as_strings_and_forbids_extras() -> None:
    schema = ReceiptAnswer.model_json_schema()
    item_schema = schema["$defs"]["ItemAnswer"]
    adjustment_schema = schema["$defs"]["AdjustmentAnswer"]

    assert [
        definition["additionalProperties"]
        for definition in (schema, item_schema, adjustment_schema)
    ] == [False, False, False]
    assert schema["properties"]["total"]["type"] == "string"
    assert item_schema["properties"]["price_paid"]["type"] == "string"
    assert adjustment_schema["properties"]["amount"]["type"] == "string"


# --- CE-TC-005, 008: printed strings become cents in code ----------------------------------------


def test_ce_tc_005_an_answer_converts_to_a_reading_in_cents() -> None:
    reading = _validate(adjusted_answer()).to_reading()

    assert reading == ReceiptReading(
        store_name=STORE_NAME,
        purchased_on="2026-11-01",
        currency="CAD",
        subtotal=cad(635),
        taxes=(cad(83),),
        total=cad(718),
        payment_last4=None,
        items=(
            ReadItem(
                printed_text="KS ORG EGGS 24CT",
                code=None,
                name="Ks Org Eggs 24Ct",
                department=None,
                quantity=Decimal(1),
                unit=Unit.EACH,
                unit_price=None,
                regular_price=cad(500),
                discount=cad(0),
                price_paid=cad(500),
            ),
            ReadItem(
                printed_text="KS SOURDOUGH",
                code=None,
                name="Ks Sourdough",
                department=None,
                quantity=Decimal(1),
                unit=Unit.EACH,
                unit_price=None,
                regular_price=cad(300),
                discount=cad(0),
                price_paid=cad(300),
            ),
        ),
        adjustments=(
            ReadAdjustment(AdjustmentKind.DEPOSIT, "BOTTLE DEPOSIT", cad(10)),
            ReadAdjustment(AdjustmentKind.FEE, "ECO FEE", cad(25)),
            ReadAdjustment(AdjustmentKind.ORDER_DISCOUNT, "MEMBER SAVINGS", cad(-200)),
        ),
    )


@pytest.mark.parametrize(
    ("printed", "cents"),
    [("4.69", 469), ("$4.69", 469), ("17.08 A", 1708)],
    ids=["plain", "dollar-sign", "tax-flag"],
)
def test_ce_tc_005_money_is_read_as_printed(printed: str, cents: int) -> None:
    reading = _validate(_one_item(regular_price=printed, price_paid=printed)).to_reading()

    assert (reading.items[0].regular_price, reading.items[0].price_paid) == (
        cad(cents),
        cad(cents),
    )


@pytest.mark.parametrize(
    ("printed", "quantity", "unit"),
    [("0.365", Decimal("0.365"), "kg"), ("2", Decimal(2), "each"), (None, Decimal(1), "lb")],
    ids=["weight", "count", "none-is-one"],
)
def test_ce_tc_005_quantity_and_unit_are_read(
    printed: str | None, quantity: Decimal, unit: str
) -> None:
    reading = _validate(_one_item(quantity=printed, unit=unit)).to_reading()

    assert (reading.items[0].quantity, reading.items[0].unit) == (quantity, Unit(unit))


@pytest.mark.parametrize("printed", ["0.30", "0.30-", "-0.30"])
def test_ce_tc_008_an_item_discount_is_its_size_whatever_its_sign(printed: str) -> None:
    payload = _one_item(regular_price="4.99", discount=printed, price_paid="4.69")

    assert _validate(payload).to_reading().items[0].discount == cad(30)


@pytest.mark.parametrize(
    ("kind", "printed", "cents"),
    [
        ("order_discount", "2.00", -200),
        ("order_discount", "2.00-", -200),
        ("order_discount", "-2.00", -200),
        ("deposit", "0.10", 10),
        ("fee", "0.25", 25),
        ("deposit", "0.10-", -10),
    ],
    ids=["order-plain", "order-trailing", "order-leading", "deposit", "fee", "bottle-return"],
)
def test_ce_tc_008_adjustments_count_with_their_sign(kind: str, printed: str, cents: int) -> None:
    payload = answer(adjustments=[adjustment(kind, "ADJUSTMENT", printed)])

    assert _validate(payload).to_reading().adjustments[0].amount == cad(cents)


def test_ce_tc_005_a_missing_discount_is_zero() -> None:
    assert _validate(answer()).to_reading().items[0].discount == cad(0)


def test_ce_tc_005_an_unreadable_value_is_named_and_quoted() -> None:
    with pytest.raises(PrintedValueError) as raised:
        _validate(_with_item(price_paid="abc")).to_reading()

    message = str(raised.value)
    assert ("abc" in message, "item 1" in message, "price_paid" in message) == (True, True, True)


def test_ce_tc_005_every_unreadable_value_is_listed_in_one_failure() -> None:
    payload = answer(total="??")
    payload["items"][0]["price_paid"] = "abc"
    payload["items"][2]["regular_price"] = "x.yz"

    with pytest.raises(PrintedValueError) as raised:
        _validate(payload).to_reading()

    message = str(raised.value)
    assert [quoted in message for quoted in ("abc", "x.yz", "??", "item 1", "item 3")] == [True] * 5


# --- the check_arithmetic tool's receipt payload (ExtractedReceipt, kept) ------------------------


def test_extracted_receipt_maps_every_field_to_a_draft() -> None:
    """PLAN 5: to_draft() maps to ReceiptDraft; the MM/DD/YYYY date is normalized."""
    receipt = ExtractedReceipt.model_validate(without_confidence(extraction()))

    draft = receipt.to_draft(today=TODAY, ocr_text=None, image_path="ab/cd/abcd.jpg")

    assert (draft.store_name, draft.purchased_on, draft.currency) == (
        STORE_NAME,
        PURCHASED_ON,
        "CAD",
    )
    assert (draft.subtotal, draft.tax, draft.total, draft.payment_last4) == (
        Money(2555, "CAD"),
        Money(128, "CAD"),
        Money(2683, "CAD"),
        "1111",
    )
    assert [(line.position, line.raw_text, line.quantity) for line in draft.items] == [
        (0, "KS ORG EGGS 24CT", Decimal(1)),
        (1, "KS 2% MILK 4L", Decimal(2)),
        (2, "KS SOURDOUGH", Decimal(2)),
    ]
    assert check_arithmetic(draft).ok


@pytest.mark.parametrize("purchased_on", [None, "", "yesterday", "12/01/2026", "2026-02-30"])
def test_extracted_receipt_stores_an_unknown_date_rather_than_failing(
    purchased_on: str | None,
) -> None:
    receipt = ExtractedReceipt.model_validate(
        without_confidence(extraction(purchased_on=purchased_on))
    )

    assert receipt.to_draft(today=TODAY, ocr_text=None, image_path=None).purchased_on is None


# --- C12: a deal price is an exact fraction per unit ----------------------------------------------


@pytest.mark.parametrize(
    ("quantity", "unit_price", "paid", "ok"),
    [
        ("6", "3/$10.00", "20.00", True),
        ("2", "2/$10.00", "10.00", True),
        ("3", "3.33", "10.00", True),
        ("12", "8.99", "8.99", False),
        ("2", "2 for 5.00", "5.00", True),
        ("2", "2 for 5.00", "7.00", False),
        ("2", "see tag", "7.00", False),
    ],
    ids=[
        "6-at-3-for-10",
        "2-at-2-for-10",
        "rounded-unit-price",
        "ks-soda-12pk",
        "2-for-5-adds-up",
        "2-for-5-against-7",
        "unreadable-unit-price-C14",
    ],
)
def test_c12_quantity_times_a_printed_unit_price_is_checked_exactly(
    quantity: str, unit_price: str, paid: str, ok: bool
) -> None:
    """Ruling C12: "N/$X" is X/N per unit, multiplied exactly before the 1-cent comparison
    (6 x 10.00/3 = 20.00, where 6 x 3.33 = 19.98 would fail)."""
    payload = _one_item(price_paid=paid, quantity=quantity, unit_price=unit_price)

    report = check_reading(_validate(payload).to_reading())

    assert [issue.code for issue in report.issues] == ([] if ok else [IssueCode.LINE_MATH])
