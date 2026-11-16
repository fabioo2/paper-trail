"""Claude's reading checked item by item and mapped to stored lines (claude-extraction plan E2,
E4; rulings CQ19, CQ22, I5, C3).

Pinned (test-cases.md CE-TC-006, 007, 010 to 014, 078):
- per item, exactly (no tolerance): regular - discount = paid (``ITEM_PRICE_MATH``, expected
  the computed price, actual the printed paid); a price paid below zero is ``NEGATIVE_ITEM``;
- a weighed item (kg, lb), or an ``each`` item with a quantity above 1 (ruling C3, which
  supersedes CQ21's weighed-only rule), that has a unit price: quantity x unit price matches
  the regular or the paid price within 1 cent (``LINE_MATH``); items without a unit price are
  not checked;
- the receipt: sum of prices paid + adjustments = subtotal (``ITEMS_VS_SUBTOTAL``), subtotal +
  taxes = total (``SUBTOTAL_PLUS_TAX_VS_TOTAL``), or without a subtotal paid + adjustments +
  taxes = total (``ITEMS_PLUS_TAX_VS_TOTAL``); regular prices and discounts never enter the
  sums; taxes are ignored when the printed subtotal is the printed total;
- the mapper writes per item an ITEM line (line total = regular price, unit price = regular /
  quantity) followed by a DISCOUNT line of -discount linked to it, then the adjustments as
  DEPOSIT, FEE or unlinked DISCOUNT lines.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paper_trail.domain.enums import Category, LineKind, Unit
from paper_trail.domain.models import LineItem, ReceiptDraft
from paper_trail.domain.money import Money
from paper_trail.domain.rules.arithmetic import (
    ArithmeticReport,
    IssueCode,
    check_arithmetic,
)
from paper_trail.domain.rules.departments import department_category
from paper_trail.domain.rules.readings import (
    AdjustmentKind,
    ReadAdjustment,
    ReadItem,
    ReceiptReading,
    check_reading,
    reading_codes,
    reading_draft,
)
from tests.factories import cad

TODAY = date(2026, 11, 6)
COSTCO = "COSTCO WHOLESALE #1234"
METRO = "METRO #0421"


def _item(  # noqa: PLR0913 - one keyword per field of a reading's item
    regular: int,
    paid: int,
    *,
    discount: int = 0,
    printed_text: str = "KS ORG EGGS 24CT",
    code: str | None = None,
    name: str | None = None,
    department: str | None = None,
    quantity: str = "1",
    unit: Unit = Unit.EACH,
    unit_price: int | None = None,
) -> ReadItem:
    return ReadItem(
        printed_text=printed_text,
        code=code,
        name=name or printed_text.title(),
        department=department,
        quantity=Decimal(quantity),
        unit=unit,
        unit_price=None if unit_price is None else cad(unit_price),
        regular_price=cad(regular),
        discount=cad(discount),
        price_paid=cad(paid),
    )


def _adjustment(
    kind: AdjustmentKind, cents: int, printed_text: str = "ADJUSTMENT"
) -> ReadAdjustment:
    return ReadAdjustment(kind=kind, printed_text=printed_text, amount=cad(cents))


def _reading(  # noqa: PLR0913 - one keyword per field of a reading
    items: tuple[ReadItem, ...] | list[ReadItem],
    *,
    total: int | None = None,
    subtotal: int | None = None,
    taxes: tuple[int, ...] = (),
    adjustments: tuple[ReadAdjustment, ...] = (),
    store_name: str = COSTCO,
) -> ReceiptReading:
    """Without a ``total`` the receipt adds up: prices paid + adjustments + taxes."""
    paid = sum(item.price_paid.amount_cents for item in items)
    paid += sum(adjustment.amount.amount_cents for adjustment in adjustments)
    return ReceiptReading(
        store_name=store_name,
        purchased_on="2026-11-01",
        currency="CAD",
        subtotal=None if subtotal is None else cad(subtotal),
        taxes=tuple(cad(tax) for tax in taxes),
        total=cad(paid + sum(taxes) if total is None else total),
        payment_last4=None,
        items=tuple(items),
        adjustments=adjustments,
    )


def _issues(report: ArithmeticReport) -> list[tuple[IssueCode, Money | None, Money | None]]:
    return [(issue.code, issue.expected, issue.actual) for issue in report.issues]


ADJUSTED_ITEMS = (_item(500, 500), _item(300, 300, printed_text="KS SOURDOUGH"))
ADJUSTMENTS = (
    _adjustment(AdjustmentKind.DEPOSIT, 10, "BOTTLE DEPOSIT"),
    _adjustment(AdjustmentKind.FEE, 25, "ECO FEE"),
    _adjustment(AdjustmentKind.ORDER_DISCOUNT, -200, "MEMBER SAVINGS"),
)


def _adjusted(*, subtotal: int | None = 635, total: int = 718) -> ReceiptReading:
    return _reading(
        ADJUSTED_ITEMS, subtotal=subtotal, taxes=(83,), total=total, adjustments=ADJUSTMENTS
    )


def _metro() -> ReceiptReading:
    """The plan's Metro receipt (test-cases.md "Fixtures")."""
    items = (
        _item(229, 229, printed_text="BANANAS", department="PRODUCE"),
        _item(279, 279, printed_text="ROMAINE HEARTS", department="PRODUCE"),
        _item(499, 469, discount=30, printed_text="WHOLE GRAIN CRACKERS", department="GROCERY"),
        _item(
            241,
            80,
            discount=161,
            printed_text="TOMATOES ON VINE",
            department="PRODUCE",
            quantity="0.365",
            unit=Unit.KG,
            unit_price=218,
        ),
        _item(1399, 1299, discount=100, printed_text="DAILY SHAMPOO", department="HBC"),
        _item(599, 549, discount=50, printed_text="PASTA SAUCE", department="GROCERY"),
        _item(1599, 1299, discount=300, printed_text="TOOTHPASTE 4PK", department="HBC"),
    )
    return _reading(items, subtotal=4204, total=4204, store_name=METRO)


# --- CE-TC-006: per item, regular - discount = paid ---------------------------------------------


@pytest.mark.parametrize(
    ("regular", "discount", "paid"),
    [(499, 30, 469), (1599, 300, 1299), (241, 161, 80), (229, 0, 229)],
    ids=["saving", "large-saving", "discount-above-paid", "no-discount"],
)
def test_ce_tc_006_an_item_that_adds_up_passes(regular: int, discount: int, paid: int) -> None:
    assert check_reading(_reading([_item(regular, paid, discount=discount)])).ok


@pytest.mark.parametrize(
    ("regular", "discount", "paid"),
    [(499, 30, 459), (499, 0, 469), (469, 30, 469), (499, 30, 468)],
    ids=["wrong-paid", "discount-not-inferred", "discount-not-applied", "one-cent-off"],
)
def test_ce_tc_006_an_item_that_does_not_add_up_fails_exactly(
    regular: int, discount: int, paid: int
) -> None:
    report = check_reading(_reading([_item(regular, paid, discount=discount)]))

    assert _issues(report) == [(IssueCode.ITEM_PRICE_MATH, cad(regular - discount), cad(paid))]
    message = report.issues[0].message
    assert ("item 1" in message, "KS ORG EGGS 24CT" in message) == (True, True)
    assert [cad(cents).amount_text in message for cents in (regular, paid)] == [True, True]


def test_ce_tc_006_a_discount_larger_than_the_regular_price_fails() -> None:
    report = check_reading(_reading([_item(100, -100, discount=200)]))

    assert [issue.code for issue in report.issues] == [IssueCode.NEGATIVE_ITEM]


# --- CE-TC-007: receipt sums ----------------------------------------------------------------------


def test_ce_tc_007_paid_prices_and_adjustments_add_up_to_subtotal_and_total() -> None:
    assert (check_reading(_adjusted()).ok, check_reading(_adjusted(subtotal=None)).ok) == (
        True,
        True,
    )


@pytest.mark.parametrize(
    ("subtotal", "total", "issues"),
    [
        (645, 728, [(IssueCode.ITEMS_VS_SUBTOTAL, cad(635), cad(645))]),
        (635, 728, [(IssueCode.SUBTOTAL_PLUS_TAX_VS_TOTAL, cad(718), cad(728))]),
        (
            645,
            718,
            [
                (IssueCode.ITEMS_VS_SUBTOTAL, cad(635), cad(645)),
                (IssueCode.SUBTOTAL_PLUS_TAX_VS_TOTAL, cad(728), cad(718)),
            ],
        ),
        (None, 728, [(IssueCode.ITEMS_PLUS_TAX_VS_TOTAL, cad(718), cad(728))]),
    ],
    ids=["subtotal", "total", "both", "no-subtotal"],
)
def test_ce_tc_007_each_wrong_sum_is_listed(
    subtotal: int | None,
    total: int,
    issues: list[tuple[IssueCode, Money, Money]],
) -> None:
    assert _issues(check_reading(_adjusted(subtotal=subtotal, total=total))) == issues


@pytest.mark.parametrize("total", [439, 499], ids=["saving-twice", "regular-price"])
def test_ce_tc_007_only_prices_paid_enter_the_receipt_sum(total: int) -> None:
    report = check_reading(_reading([_item(499, 469, discount=30)], total=total))

    assert _issues(report) == [(IssueCode.ITEMS_PLUS_TAX_VS_TOTAL, cad(469), cad(total))]


def test_ce_tc_007_taxes_are_ignored_when_the_subtotal_is_the_total() -> None:
    """CQ22: a tax-inclusive receipt lists its taxes but prints subtotal = total."""
    reading = _reading([_item(500, 500)], subtotal=500, taxes=(65,), total=500)

    assert check_reading(reading).ok


_PRICES = st.integers(min_value=0, max_value=50_000)


@given(
    st.lists(st.tuples(_PRICES, _PRICES), min_size=1, max_size=8),
    st.lists(st.integers(min_value=-2_000, max_value=2_000), max_size=3),
)
def test_ce_tc_007_the_receipt_sum_is_paid_prices_plus_adjustments(
    prices: list[tuple[int, int]], amounts: list[int]
) -> None:
    items = [_item(regular + saving, regular, discount=saving) for regular, saving in prices]
    adjustments = tuple(_adjustment(AdjustmentKind.FEE, cents) for cents in amounts)
    expected = sum(regular for regular, _ in prices) + sum(amounts)

    assert check_reading(_reading(items, total=expected, adjustments=adjustments)).ok
    assert not check_reading(_reading(items, total=expected + 1, adjustments=adjustments)).ok


# --- CE-TC-010, 011, 012: discounts, adjustments, multi-buys ------------------------------------

CHOC = "1362394 CHOC PISTACH"


@pytest.mark.parametrize(
    ("items", "adjustments", "total", "issues"),
    [
        ([_item(1799, 1399, discount=400, printed_text=CHOC)], (), 1399, []),
        (
            [_item(1799, 1399, discount=400, printed_text=CHOC)],
            (),
            1799,
            [(IssueCode.ITEMS_PLUS_TAX_VS_TOTAL, cad(1399), cad(1799))],
        ),
        (
            [_item(1799, 1799, printed_text=CHOC)],
            (_adjustment(AdjustmentKind.ORDER_DISCOUNT, -400),),
            1399,
            [],
        ),
        (
            [_item(1799, 1399, discount=400, printed_text=CHOC)],
            (_adjustment(AdjustmentKind.ORDER_DISCOUNT, -400),),
            1399,
            [(IssueCode.ITEMS_PLUS_TAX_VS_TOTAL, cad(999), cad(1399))],
        ),
    ],
    ids=["item-discount", "total-is-regular", "order-discount", "discounted-twice"],
)
def test_ce_tc_010_costco_discounts_in_the_new_shape(
    items: list[ReadItem],
    adjustments: tuple[ReadAdjustment, ...],
    total: int,
    issues: list[tuple[IssueCode, Money, Money]],
) -> None:
    assert _issues(check_reading(_reading(items, total=total, adjustments=adjustments))) == issues


def test_ce_tc_011_deposits_and_fees_count_in_the_receipt_sum_only() -> None:
    adjustments = ADJUSTMENTS[:2]

    assert check_reading(_reading([_item(899, 899)], total=934, adjustments=adjustments)).ok
    assert _issues(
        check_reading(_reading([_item(899, 899)], total=899, adjustments=adjustments))
    ) == [(IssueCode.ITEMS_PLUS_TAX_VS_TOTAL, cad(934), cad(899))]


def test_ce_tc_011_an_item_cannot_reduce_the_receipt() -> None:
    report = check_reading(_reading([_item(899, 899), _item(-200, -200)]))

    assert [issue.code for issue in report.issues] == [IssueCode.NEGATIVE_ITEM]


def test_ce_tc_011_a_receipt_of_adjustments_only_is_not_valid() -> None:
    report = check_reading(_reading([], adjustments=ADJUSTMENTS[:1]))

    assert IssueCode.EMPTY_RECEIPT in [issue.code for issue in report.issues]


@pytest.mark.parametrize(
    "items",
    [
        [_item(500, 500), _item(500, 500)],
        [_item(1000, 1000, quantity="2")],
        [_item(1000, 1000), _item(0, 0)],
    ],
    ids=["split", "one-line-quantity-2", "all-on-the-first"],
)
def test_ce_tc_012_a_multi_buy_needs_no_special_rule(items: list[ReadItem]) -> None:
    assert check_reading(_reading(items, total=1000)).ok


# --- CE-TC-013: the Metro receipt -----------------------------------------------------------------


def test_ce_tc_013_the_metro_receipt_validates_to_its_printed_figures() -> None:
    reading = _metro()

    assert check_reading(reading) == ArithmeticReport(())
    assert (
        sum(item.price_paid.amount_cents for item in reading.items),
        sum(item.regular_price.amount_cents for item in reading.items),
        sum(item.discount.amount_cents for item in reading.items),
    ) == (4204, 4845, 641)


# --- CE-TC-014: weighed items ---------------------------------------------------------------------


@pytest.mark.parametrize("unit", [Unit.KG, Unit.LB])
@pytest.mark.parametrize(("regular", "ok"), [(152, True), (153, True), (151, True), (154, False)])
def test_ce_tc_014_a_weighed_item_matches_quantity_times_unit_price_within_a_cent(
    unit: Unit, regular: int, ok: bool
) -> None:
    weighed = _item(regular, regular, quantity="0.460", unit=unit, unit_price=331)

    report = check_reading(_reading([weighed]))

    assert [issue.code for issue in report.issues] == ([] if ok else [IssueCode.LINE_MATH])
    if not ok:
        assert "item 1" in report.issues[0].message


@pytest.mark.parametrize(
    ("item", "ok"),
    [
        (_item(662, 662, quantity="2", unit=Unit.EACH, unit_price=331), True),
        (_item(899, 899, quantity="12", unit=Unit.EACH, unit_price=899), False),
        (_item(999, 999, quantity="2", unit=Unit.EACH, unit_price=None), True),
        (_item(999, 999, quantity="0.460", unit=Unit.KG, unit_price=None), True),
        (_item(899, 899, quantity="1", unit=Unit.EACH, unit_price=750), True),
    ],
    ids=["each-adds-up", "ks-soda-12pk", "each-no-unit-price", "kg-no-unit-price", "quantity-1"],
)
def test_c3_an_item_with_a_unit_price_and_quantity_above_1_must_add_up(
    item: ReadItem, ok: bool
) -> None:
    """Ruling C3 (supersedes CQ21's weighed-only rule): each items too. The pack size read as a
    quantity (KS SODA 12PK: 12 x 8.99 for a line of 8.99) is caught; one unit is not checked."""
    report = check_reading(_reading([item]))

    assert [issue.code for issue in report.issues] == ([] if ok else [IssueCode.LINE_MATH])


@pytest.mark.parametrize(
    ("regular", "discount", "paid", "ok"),
    [(241, 161, 80, True), (251, 161, 90, False)],
    ids=["matches-paid", "matches-neither"],
)
def test_ce_tc_014_a_discounted_weighed_item_may_match_the_paid_price(
    regular: int, discount: int, paid: int, ok: bool
) -> None:
    """CQ19: Metro prints the paid unit price (0.365 x 2.18 = 0.80, not the regular 2.41)."""
    weighed = _item(
        regular, paid, discount=discount, quantity="0.365", unit=Unit.KG, unit_price=218
    )

    report = check_reading(_reading([weighed]))

    assert [issue.code for issue in report.issues] == ([] if ok else [IssueCode.LINE_MATH])


# --- CE-TC-015: department headings ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("store_key", "department", "category"),
    [
        ("metro", "PRODUCE", Category.PRODUCE),
        ("metro", "HBC", Category.PERSONAL_CARE),
        ("metro", " produce ", Category.PRODUCE),
        ("metro", "SEASONAL XYZ", None),
        ("costco", "PRODUCE", None),
        ("metro", None, None),
    ],
    ids=["produce", "hbc", "case-and-spaces", "unknown-heading", "store-without-map", "none"],
)
def test_ce_tc_015_a_department_heading_is_a_category_hint_per_store(
    store_key: str, department: str | None, category: Category | None
) -> None:
    assert department_category(store_key, department) is category


# --- CE-TC-078: the storage mapper ----------------------------------------------------------------


def _line(  # noqa: PLR0913 - one keyword per field of a stored line
    position: int,
    kind: LineKind,
    raw_text: str,
    total: int,
    *,
    description: str | None = None,
    quantity: str = "1",
    unit_price: int | None = None,
    category: Category | None = None,
    applies_to: int | None = None,
) -> LineItem:
    return LineItem(
        position=position,
        kind=kind,
        raw_text=raw_text,
        description=description or raw_text,
        quantity=Decimal(quantity),
        unit_price=None if unit_price is None else cad(unit_price),
        line_total=cad(total),
        category=category,
        applies_to_position=applies_to,
    )


ITEM = LineKind.ITEM
DISCOUNT = LineKind.DISCOUNT


def test_ce_tc_078_items_adjustments_and_receipt_fields_map_to_a_draft() -> None:
    reading = _adjusted()
    discounted = replace(reading, items=(_item(499, 469, discount=30), *reading.items[1:]))

    draft = reading_draft(discounted, today=TODAY, image_path="ab/cd/abcd.jpg")

    assert draft == ReceiptDraft(
        store_name=COSTCO,
        purchased_on=date(2026, 11, 1),
        currency="CAD",
        subtotal=cad(635),
        tax=cad(83),
        total=cad(718),
        items=(
            _line(0, ITEM, "KS ORG EGGS 24CT", 499, description="Ks Org Eggs 24Ct", unit_price=499),
            _line(
                1, DISCOUNT, "KS ORG EGGS 24CT", -30, description="Ks Org Eggs 24Ct", applies_to=0
            ),
            _line(2, ITEM, "KS SOURDOUGH", 300, description="Ks Sourdough", unit_price=300),
            _line(3, LineKind.DEPOSIT, "BOTTLE DEPOSIT", 10),
            _line(4, LineKind.FEE, "ECO FEE", 25),
            _line(5, DISCOUNT, "MEMBER SAVINGS", -200),
        ),
        payment_last4=None,
        ocr_text=None,
        image_path="ab/cd/abcd.jpg",
    )


def test_ce_tc_078_the_metro_receipt_maps_to_items_and_linked_discounts() -> None:
    draft = reading_draft(_metro(), today=TODAY, image_path=None)

    produce, care = Category.PRODUCE, Category.PERSONAL_CARE
    crackers, tomatoes, shampoo = "WHOLE GRAIN CRACKERS", "TOMATOES ON VINE", "DAILY SHAMPOO"
    sauce, toothpaste = "PASTA SAUCE", "TOOTHPASTE 4PK"
    assert draft.items == (
        _line(0, ITEM, "BANANAS", 229, description="Bananas", unit_price=229, category=produce),
        _line(
            1,
            ITEM,
            "ROMAINE HEARTS",
            279,
            description="Romaine Hearts",
            unit_price=279,
            category=produce,
        ),
        _line(2, ITEM, crackers, 499, description=crackers.title(), unit_price=499),
        _line(3, DISCOUNT, crackers, -30, description=crackers.title(), applies_to=2),
        _line(
            4,
            ITEM,
            tomatoes,
            241,
            description=tomatoes.title(),
            quantity="0.365",
            unit_price=660,
            category=produce,
        ),
        _line(5, DISCOUNT, tomatoes, -161, description=tomatoes.title(), applies_to=4),
        _line(6, ITEM, shampoo, 1399, description=shampoo.title(), unit_price=1399, category=care),
        _line(7, DISCOUNT, shampoo, -100, description=shampoo.title(), applies_to=6),
        _line(8, ITEM, sauce, 599, description=sauce.title(), unit_price=599),
        _line(9, DISCOUNT, sauce, -50, description=sauce.title(), applies_to=8),
        _line(
            10,
            ITEM,
            toothpaste,
            1599,
            description=toothpaste.title(),
            unit_price=1599,
            category=care,
        ),
        _line(11, DISCOUNT, toothpaste, -300, description=toothpaste.title(), applies_to=10),
    )
    assert (draft.subtotal, draft.tax, draft.total) == (cad(4204), cad(0), cad(4204))


@pytest.mark.parametrize("reading", [_adjusted(), _metro()], ids=["adjusted", "metro"])
def test_ce_tc_078_mapped_lines_pass_todays_checks_and_sum_to_the_subtotal(
    reading: ReceiptReading,
) -> None:
    draft = reading_draft(reading, today=TODAY, image_path=None)

    assert check_arithmetic(draft).ok
    assert draft.subtotal is not None
    assert sum(line.line_total.amount_cents for line in draft.items) == draft.subtotal.amount_cents


def test_r1_a_discount_larger_than_its_item_is_mapped_unlinked() -> None:
    """Ruling R1: the mapper is total; a reading the validator refuses still maps (review
    builds its draft with it), the discount left unlinked instead of raising."""
    draft = reading_draft(_reading([_item(100, -100, discount=200)]), today=TODAY, image_path=None)

    assert draft.items == (
        _line(0, ITEM, "KS ORG EGGS 24CT", 100, description="Ks Org Eggs 24Ct", unit_price=100),
        _line(1, DISCOUNT, "KS ORG EGGS 24CT", -200, description="Ks Org Eggs 24Ct"),
    )


def test_ce_tc_078_an_item_without_a_discount_gets_no_discount_line() -> None:
    draft = reading_draft(_reading([_item(899, 899)]), today=TODAY, image_path=None)

    assert [line.kind for line in draft.items] == [ITEM]


def test_ce_tc_079_codes_are_keyed_by_the_position_of_their_item_line() -> None:
    reading = _reading(
        [
            _item(1799, 1399, discount=400, printed_text=CHOC, code="1362394"),
            _item(899, 899, printed_text="KS ORG EGGS 24CT"),
            _item(499, 499, printed_text="036000291452 CEREAL", code="036000291452"),
        ]
    )

    assert dict(reading_codes(reading)) == {0: "1362394", 3: "036000291452"}
