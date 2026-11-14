"""Tying a discount line to the item it reduces (discounts and paid prices, R1-R4, A1, A3, A4).

Pinned, in ``paper_trail.domain.rules.discounts`` (pure):

- ``discount_reference(raw_text)``: the code a discount names (B of ``A / B`` when B is 4-13
  digits, else A when A is), ``"MULTIPLE"``, or None (P1).
- ``attribute_discounts(lines)``: every DISCOUNT line once, linked to the nearest preceding
  untaken ITEM line with its code (leading zeros aside) or kept at receipt level with a
  ``Refusal``; links already on the lines are kept and reserve their item (P2).
- ``with_attribution(lines, found)``: the lines with the new links set, nothing else changed.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paper_trail.domain.enums import LineKind
from paper_trail.domain.models import LineItem
from paper_trail.domain.rules.arithmetic import check_arithmetic
from paper_trail.domain.rules.discounts import (
    MULTIPLE,
    DiscountAttribution,
    DiscountLink,
    ReceiptLevelDiscount,
    Refusal,
    attribute_discounts,
    discount_reference,
    with_attribution,
)
from tests.factories import (
    CHOC,
    KIWI,
    PRINTED_LINKS,
    PRINTED_MULTIPLE,
    cad,
    discount_line,
    draft_with,
    item_line,
    line_item,
    printed_lines,
    simple_lines,
)

COUPON = "0000000001"

# --- P1 DISCOUNT_REFERENCES ---------------------------------------------------------------------

DISCOUNT_REFERENCES = [
    pytest.param(f"{COUPON} / 1543210", "1543210", id="TC-01-coupon-then-code"),
    pytest.param(f"{COUPON} /1234567", "1234567", id="TC-01-space-only-before-slash"),
    pytest.param("1543210 / 1362394", "1362394", id="TC-01-both-sides-digits-takes-b"),
    pytest.param("TPD/1362394", "1362394", id="TC-02-tpd"),
    pytest.param("1362394/TPD", "1362394", id="TC-04-code-then-tpd"),
    pytest.param("MULTIPLE", MULTIPLE, id="TC-05-multiple"),
    pytest.param(f"{COUPON} / MULTIPLE", MULTIPLE, id="TC-05-coupon-multiple"),
    pytest.param(f"{COUPON} / multiple", MULTIPLE, id="TC-05-multiple-any-case"),
    pytest.param("Instant savings", None, id="TC-06-no-code"),
    pytest.param("TPD", None, id="TC-06-tpd-alone"),
    pytest.param("", None, id="TC-06-empty"),
    pytest.param("TPD/1281", "1281", id="TC-108-4-digits"),
    pytest.param("TPD/0000001362394", "0000001362394", id="TC-108-13-digits"),
    pytest.param("1543210 / 12", "1543210", id="TC-108-b-2-digits"),
    pytest.param("1543210 / 12345678901234", "1543210", id="TC-108-b-14-digits"),
    pytest.param("TPD/123", None, id="TC-108-b-3-digits"),
    pytest.param("12/34", None, id="TC-108-neither-side"),
]


@pytest.mark.parametrize(("raw_text", "expected"), DISCOUNT_REFERENCES)
def test_discount_reference(raw_text: str, expected: str | None) -> None:
    assert discount_reference(raw_text) == expected


# --- P2 LINKING ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Kept:
    """An explicit link the line carried in, reported as kept (``new=False``)."""

    item_position: int


type Spec = tuple[LineKind, str, int, str, int | None]
type Outcome = int | Kept | Refusal


def it(raw_text: str, cents: int, quantity: str = "1") -> Spec:
    return (LineKind.ITEM, raw_text, cents, quantity, None)


def dc(raw_text: str, cents: int, to: int | None = None) -> Spec:
    return (LineKind.DISCOUNT, raw_text, cents, "1", to)


def other(kind: LineKind, raw_text: str, cents: int) -> Spec:
    return (kind, raw_text, cents, "1", None)


def build(specs: Sequence[Spec]) -> tuple[LineItem, ...]:
    return tuple(
        line_item(
            position=position,
            kind=kind,
            raw_text=raw_text,
            description=raw_text,
            quantity=Decimal(quantity),
            unit_price=None,
            line_total=cad(cents),
            applies_to_position=to,
        )
        for position, (kind, raw_text, cents, quantity, to) in enumerate(specs)
    )


def outcomes(found: DiscountAttribution) -> dict[int, Outcome]:
    linked: dict[int, Outcome] = {
        link.discount_position: link.item_position if link.new else Kept(link.item_position)
        for link in found.links
    }
    refused: dict[int, Outcome] = {
        level.discount_position: level.reason for level in found.receipt_level
    }
    return linked | refused


def expected_lines(lines: Sequence[LineItem], new: Mapping[int, Outcome]) -> tuple[LineItem, ...]:
    """The input with only the new links set."""
    return tuple(
        replace(line, applies_to_position=target)
        if isinstance(target := new.get(line.position), int)
        else line
        for line in lines
    )


TPD_CHOC, TPD_KIWI = "TPD/1362394", "TPD/1543210"
WEIGHED = "1234567 BANANAS"

LINKING = [
    pytest.param([it(KIWI, 1599), dc(TPD_KIWI, -400)], {1: 0}, id="TC-13-one-pair"),
    pytest.param([it(CHOC, 1699), it(CHOC, 1699), dc(TPD_CHOC, -400)], {2: 1}, id="TC-14-nearest"),
    pytest.param(
        [it(CHOC, 1699), it(CHOC, 1699), dc(TPD_CHOC, -400), dc(TPD_CHOC, -400)],
        {2: 1, 3: 0},
        id="TC-15-next-nearest-untaken",
    ),
    pytest.param(
        [it("(2) 1362394 CHOC PISTACH", 3398, "2"), dc(TPD_CHOC, -400)],
        {1: 0},
        id="TC-17-quantity-marker",
    ),
    pytest.param(
        [it(CHOC, 1699), dc("TPD/0001362394", -400)], {1: 0}, id="TC-07-zeros-on-discount"
    ),
    pytest.param(
        [it("0001362394 CHOC PISTACH", 1699), dc(TPD_CHOC, -400)],
        {1: 0},
        id="TC-07-zeros-on-item",
    ),
    pytest.param(
        [it(CHOC, 1699), dc("MULTIPLE", -600)], {1: Refusal.MULTIPLE}, id="TC-18-multiple"
    ),
    pytest.param(
        [it(CHOC, 1699), dc("Instant savings", -400)],
        {1: Refusal.NO_REFERENCE},
        id="TC-19-no-reference",
    ),
    pytest.param(
        [it("1695232 KIMCHI", 1099), dc(TPD_CHOC, -400)], {1: Refusal.NO_ITEM}, id="TC-20"
    ),
    pytest.param(
        [dc(TPD_CHOC, -400), it(CHOC, 1699)], {0: Refusal.NO_ITEM}, id="TC-21-never-later"
    ),
    pytest.param(
        [it(CHOC, 1699), dc(TPD_CHOC, -400), dc(TPD_CHOC, -400)],
        {1: 0, 2: Refusal.NO_ITEM},
        id="TC-22-item-taken",
    ),
    pytest.param(
        [it(KIWI, 1599), it(KIWI, 399), dc(TPD_KIWI, -400)],
        {2: Refusal.EXCEEDS_ITEM},
        id="TC-23-no-fallback",
    ),
    pytest.param([it(KIWI, 399), dc(TPD_KIWI, -399)], {1: 0}, id="TC-24-equal-links"),
    pytest.param(
        [it("1234567 KS 2% MILK", 658, "2"), dc("TPD/1234567", -400)],
        {1: 0},
        id="TC-25-line-total-not-unit-price",
    ),
    pytest.param([it(WEIGHED, 204, "1.36"), dc("TPD/1234567", -204)], {1: 0}, id="TC-25-weighed"),
    pytest.param(
        [it(WEIGHED, 204, "1.36"), dc("TPD/1234567", -205)],
        {1: Refusal.EXCEEDS_ITEM},
        id="TC-25-weighed-exceeds",
    ),
    pytest.param(
        [
            it("1362394 CHOC", 1699),
            other(LineKind.DEPOSIT, "1362394 DEPOSIT", 10),
            other(LineKind.FEE, "1362394 FEE", 50),
            dc("1362394 / MULTIPLE", -100),
            dc(TPD_CHOC, -400),
        ],
        {3: Refusal.MULTIPLE, 4: 0},
        id="TC-27-only-items",
    ),
    pytest.param(
        [
            other(LineKind.DEPOSIT, "1362394 DEPOSIT", 10),
            other(LineKind.FEE, "1362394 FEE", 50),
            dc("1362394 / MULTIPLE", -100),
            dc(TPD_CHOC, -400),
        ],
        {2: Refusal.MULTIPLE, 3: Refusal.NO_ITEM},
        id="TC-27-no-item",
    ),
    pytest.param([it(CHOC, 1699), dc(TPD_CHOC, 0)], {1: Refusal.NOT_A_SAVING}, id="TC-109-zero"),
    pytest.param(
        [it(CHOC, 1699), dc(TPD_CHOC, 400)], {1: Refusal.NOT_A_SAVING}, id="TC-109-positive"
    ),
    pytest.param(
        [it(CHOC, 1699), it(CHOC, 1699), dc(TPD_CHOC, -400, to=0)],
        {2: Kept(0)},
        id="TC-32a-explicit-kept",
    ),
    pytest.param(
        [dc(TPD_CHOC, -400, to=1), it(KIWI, 1599)],
        {0: Kept(1)},
        id="TC-32b-explicit-later-other-code",
    ),
    pytest.param(
        [it(CHOC, 1699), dc("Instant savings", -400, to=0)],
        {1: Kept(0)},
        id="TC-32c-explicit-without-code",
    ),
    pytest.param(
        [it(CHOC, 1699), dc(TPD_CHOC, -400), dc(TPD_CHOC, -400, to=0)],
        {1: Refusal.NO_ITEM, 2: Kept(0)},
        id="TC-33-explicit-reserved-first",
    ),
    pytest.param(
        [it(KIWI, 399), dc(TPD_KIWI, -400), dc(TPD_KIWI, -300)],
        {1: Refusal.NO_ITEM, 2: 0},
        id="Q22-refused-exceeds-leaves-item-free",
    ),
    pytest.param(
        [it(KIWI, 1599), it(KIWI, 399), dc(TPD_KIWI, -400), dc(TPD_KIWI, -300)],
        {2: 0, 3: 1},
        id="TC-33-TC-34-stable-once-the-later-discount-takes-its-item",
    ),
    pytest.param(
        [it(KIWI, 1599), dc(TPD_KIWI, 0), dc(TPD_KIWI, -400)],
        {1: Refusal.NOT_A_SAVING, 2: 0},
        id="Q22-not-a-saving-leaves-item-free",
    ),
    pytest.param(
        [it(CHOC, 1699), dc("MULTIPLE", 0)],
        {1: Refusal.NOT_A_SAVING},
        id="Q23-not-a-saving-before-multiple",
    ),
    pytest.param(
        [it(CHOC, 1699), dc("Instant savings", 100)],
        {1: Refusal.NOT_A_SAVING},
        id="Q23-not-a-saving-before-no-reference",
    ),
]


@pytest.mark.parametrize(("specs", "expected"), LINKING)
def test_linking(specs: list[Spec], expected: dict[int, Outcome]) -> None:
    lines = build(specs)

    found = attribute_discounts(lines)

    assert outcomes(found) == expected
    assert with_attribution(lines, found) == expected_lines(lines, expected)


def test_tc_16_the_printed_receipt() -> None:
    lines = printed_lines()

    found = attribute_discounts(lines)

    links = tuple(
        DiscountLink(discount, item, lines[discount].raw_text.removeprefix("TPD/"), new=True)
        for discount, item in PRINTED_LINKS.items()
    )
    multiple = ReceiptLevelDiscount(PRINTED_MULTIPLE, Refusal.MULTIPLE, MULTIPLE)
    assert found == DiscountAttribution(links, (multiple,))
    assert found.new_links == links
    assert with_attribution(lines, found) == printed_lines(PRINTED_LINKS)


def test_tc_114_links_on_the_lines_are_reported_as_kept() -> None:
    lines = printed_lines({6: 5, 8: 7})

    found = attribute_discounts(lines)

    links = tuple(
        DiscountLink(
            discount, item, lines[discount].raw_text.removeprefix("TPD/"), new=discount > 8
        )
        for discount, item in PRINTED_LINKS.items()
    )
    multiple = ReceiptLevelDiscount(PRINTED_MULTIPLE, Refusal.MULTIPLE, MULTIPLE)
    assert found == DiscountAttribution(links, (multiple,))
    assert (len(found.links), len(found.receipt_level), len(found.new_links)) == (7, 1, 5)
    assert with_attribution(lines, found) == printed_lines(PRINTED_LINKS)


def test_tc_35_a_receipt_without_discounts() -> None:
    lines = simple_lines()[:4]

    found = attribute_discounts(lines)

    assert found == DiscountAttribution((), ())
    assert found.new_links == ()
    assert with_attribution(lines, found) == lines


# --- properties (TC-30, TC-31, TC-34) -----------------------------------------------------------

CODES = ["1362394", "1543210", "1234567", "1695232", "1281"]


@dataclass(frozen=True)
class Generated:
    lines: tuple[LineItem, ...]
    codes: dict[int, str]  # the code each ITEM and coded DISCOUNT line was generated with


@st.composite
def receipts(draw: st.DrawFn) -> Generated:
    lines: list[LineItem] = []
    codes: dict[int, str] = {}
    for position in range(draw(st.integers(min_value=0, max_value=12))):
        kind = draw(st.sampled_from(list(LineKind)))
        code = draw(st.sampled_from(CODES))
        printed = draw(st.sampled_from(["", "0", "000"])) + code
        if kind is LineKind.DISCOUNT:
            shape = draw(st.sampled_from(["TPD/{}", "0000000001 / {}", "MULTIPLE", "TPD"]))
            cents = draw(st.integers(min_value=-3000, max_value=500))
            line = discount_line(position, shape.format(printed), cents)
            if "{}" in shape:
                codes[position] = code
        else:
            cents = draw(st.integers(min_value=0, max_value=3000))
            line = item_line(position, f"{printed} THING", cents, kind=kind)
            if kind is LineKind.ITEM:
                codes[position] = code
        lines.append(line)
    return Generated(tuple(lines), codes)


@given(receipts())
def test_tc_30_links_are_exact_and_nothing_else_changes(generated: Generated) -> None:
    lines = generated.lines

    found = attribute_discounts(lines)
    linked = with_attribution(lines, found)

    assert len(linked) == len(lines)
    assert [replace(line, applies_to_position=None) for line in linked] == list(lines)
    discounts = [line.position for line in lines if line.kind is LineKind.DISCOUNT]
    reported = [link.discount_position for link in found.links]
    reported += [level.discount_position for level in found.receipt_level]
    assert sorted(reported) == discounts
    targets = [line.applies_to_position for line in linked if line.applies_to_position is not None]
    assert len(targets) == len(set(targets))
    for line in linked:
        if line.applies_to_position is None:
            continue
        item = linked[line.applies_to_position]
        assert line.kind is LineKind.DISCOUNT
        assert line.line_total.amount_cents < 0
        assert item.kind is LineKind.ITEM
        assert item.position < line.position
        assert generated.codes[item.position] == generated.codes[line.position]
        assert item.line_total.amount_cents + line.line_total.amount_cents >= 0


@given(receipts(), st.integers(min_value=-500, max_value=500))
def test_tc_31_linking_never_changes_the_arithmetic(generated: Generated, offset: int) -> None:
    items_total = sum(line.line_total.amount_cents for line in generated.lines)
    draft = draft_with(generated.lines, subtotal=items_total + offset, tax=0, total=items_total)
    linked = replace(draft, items=with_attribution(draft.items, attribute_discounts(draft.items)))

    assert check_arithmetic(linked) == check_arithmetic(draft)


@given(receipts())
def test_tc_34_attributing_the_output_again_changes_nothing(generated: Generated) -> None:
    first = attribute_discounts(generated.lines)

    again = attribute_discounts(with_attribution(generated.lines, first))

    assert again == DiscountAttribution(
        tuple(replace(link, new=False) for link in first.links), first.receipt_level
    )
    assert again.new_links == ()
