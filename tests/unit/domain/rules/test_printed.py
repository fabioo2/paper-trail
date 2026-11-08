"""Printed values become numbers in code, never in the model (EA-AC-01..03, ADR-0017).

docs/plans/extraction-accuracy.md 2.2: the model copies amounts, quantities and unit prices as
printed; these pure parsers in ``paper_trail.domain.rules.printed`` turn them into signed integer
cents and ``Decimal`` quantities. ``PrintedValueError`` (``paper_trail.domain.errors``) quotes
the printed text it could not read.

Pinned:
- ``parse_amount(text) -> int`` signed cents: optional ``$``, spaces, thousands ``,``, decimal
  ``.`` or ``,`` with exactly two fraction digits, 1-3 trailing tax-flag letters ignored;
  negative for a leading ``-`` or U+2212, a trailing ``-``, a trailing ``CR`` or parentheses.
- ``parse_quantity(text | None) -> Decimal`` (> 0): None or blank is 1; units ``kg``, ``lb``,
  ``ea`` and ``@``/``x`` markers around the number are dropped.
- ``parse_unit_price(text | None) -> int | None`` is lenient: unreadable is None; ``N/$X`` is
  X/N rounded half-up.
- ``line_kind(kind, cents) -> (LineKind, cents)``: negative or ``discount`` -> DISCOUNT with a
  negative amount; anything else as given.
"""

from decimal import Decimal

import pytest

from paper_trail.domain.enums import LineKind
from paper_trail.domain.errors import PrintedValueError
from paper_trail.domain.rules.printed import (
    TaxReading,
    is_item_reference,
    line_kind,
    line_quantity,
    parse_amount,
    parse_quantity,
    parse_tax,
    parse_unit_price,
    printed_tax,
)

# --- EA-AC-01: amounts ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("1.29", 129),
        ("1,29", 129),  # decimal comma
        ("1,299.00", 129900),  # thousands separator
        ("$4.00", 400),
        ("$ 4.00", 400),
        (" 8.99 ", 899),
        ("0.00", 0),
        ("4.00-", -400),  # Costco's trailing minus (evidence 3b)
        ("-4.00", -400),
        ("\N{MINUS SIGN}4.00", -400),  # a typographic minus sign
        ("4.00 CR", -400),
        ("(4.00)", -400),
        ("17.08 A", 1708),  # a tax flag after the amount
        ("18.99 H", 1899),
        ("6.58 GST", 658),
        ("1.29 MRJ", 129),
    ],
)
def test_ea_ac_01_amounts_as_printed_become_signed_cents(text: str, cents: int) -> None:
    assert parse_amount(text) == cents


@pytest.mark.parametrize(
    "text",
    [
        "1290",  # the vision model's 10x answer: no cents printed (evidence 3a)
        "1.2",  # one fraction digit
        "1.299",  # three fraction digits
        "",
        "   ",
        "abc",
        "4.00 TOTAL",  # more than a tax flag after the amount
    ],
)
def test_ea_ac_01_anything_else_is_an_error_quoting_the_text(text: str) -> None:
    with pytest.raises(PrintedValueError) as raised:
        parse_amount(text)

    assert f'"{text}"' in str(raised.value)


# --- EA-AC-02: quantities and unit prices --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "quantity"),
    [
        (None, Decimal(1)),
        ("", Decimal(1)),
        ("  ", Decimal(1)),
        ("2", Decimal(2)),
        ("0.460 kg", Decimal("0.460")),
        ("1.36 lb", Decimal("1.36")),
        ("2 @", Decimal(2)),
        ("3 ea", Decimal(3)),
        ("2x", Decimal(2)),
    ],
)
def test_ea_ac_02_quantities(text: str | None, quantity: Decimal) -> None:
    parsed = parse_quantity(text)

    assert isinstance(parsed, Decimal)
    assert parsed == quantity


@pytest.mark.parametrize("text", ["0", "-1", "x", "kg", "two"])
def test_ea_ac_02_a_quantity_that_is_not_positive_is_an_error(text: str) -> None:
    with pytest.raises(PrintedValueError) as raised:
        parse_quantity(text)

    assert f'"{text}"' in str(raised.value)


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("1.29", 129),
        ("$3.31/kg", 331),
        ("$2.20/lb", 220),
        ("@ 3.29 ea", 329),
        ("2/$10.00", 500),
        ("3/$10.00", 333),  # 3.333... rounded half-up
        ("3/$5.00", 167),  # 1.666... rounded half-up
        (None, None),
        ("", None),
        ("abc", None),  # junk: the unit price is optional, so it is dropped, not an error
        ("lmt 4", None),
    ],
)
def test_ea_ac_02_unit_prices_are_parsed_leniently(text: str | None, cents: int | None) -> None:
    assert parse_unit_price(text) == cents


# --- EA-AC-03: sign and kind ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "cents", "expected"),
    [
        (LineKind.ITEM, -400, (LineKind.DISCOUNT, -400)),  # an "item" printed 4.00-
        (LineKind.DISCOUNT, 400, (LineKind.DISCOUNT, -400)),  # a discount printed 4.00
        (LineKind.DISCOUNT, -400, (LineKind.DISCOUNT, -400)),
        (LineKind.ITEM, 899, (LineKind.ITEM, 899)),
        (LineKind.DEPOSIT, 10, (LineKind.DEPOSIT, 10)),
        (LineKind.FEE, 25, (LineKind.FEE, 25)),
    ],
)
def test_ea_ac_03_the_sign_decides_the_kind(
    kind: LineKind, cents: int, expected: tuple[LineKind, int]
) -> None:
    assert line_kind(kind, cents) == expected


@pytest.mark.parametrize("kind", list(LineKind))
@pytest.mark.parametrize("cents", [-1, -400, -129900])
def test_ea_ac_03_a_negative_line_is_always_a_discount(kind: LineKind, cents: int) -> None:
    """Property: a negative charge (item, deposit, fee) cannot survive the conversion."""
    assert line_kind(kind, cents) == (LineKind.DISCOUNT, cents)


# --- fix round (review findings 4 and 6, real-run findings 7 and 8) ------------------------------


def test_review_4_a_four_letter_tax_flag_is_ignored() -> None:
    assert parse_amount("6.58 GSTH") == 658


def test_review_4_five_letters_are_not_a_tax_flag() -> None:
    with pytest.raises(PrintedValueError):
        parse_amount("6.58 TOTAL")


@pytest.mark.parametrize(
    ("text", "quantity"),
    [
        ("0.225 kg @ $7.69/kg", Decimal("0.225")),  # the whole weighed line
        ("0.460kg @ 3.31/kg", Decimal("0.460")),
        ("2 @ $3.29", Decimal(2)),
        ("2 @ 2/$10.00", Decimal(2)),
    ],
)
def test_review_6_a_whole_detail_line_gives_its_leading_quantity(
    text: str, quantity: Decimal
) -> None:
    assert parse_quantity(text) == quantity


@pytest.mark.parametrize(
    ("quantity", "unit", "cents", "expected"),
    [
        (Decimal("1.5"), 1099, 1099, Decimal(1)),  # "KIMCHI 1.5KG": the size, not a quantity
        (Decimal(15), 1099, 1099, Decimal(1)),  # "DOUGHNUTS 15": a count in the name
        (Decimal(2), 329, 658, Decimal(2)),
        (Decimal("0.460"), 331, 152, Decimal("0.460")),
        (Decimal(3), None, 999, Decimal(3)),
        (Decimal(2), 500, None, Decimal(2)),  # no amount printed (a multi-buy group)
    ],
)
def test_real_run_7_a_unit_price_equal_to_the_amount_means_one_unit(
    quantity: Decimal, unit: int | None, cents: int | None, expected: Decimal
) -> None:
    assert line_quantity(quantity, unit, cents) == expected


@pytest.mark.parametrize(
    "raw_text",
    ["0000392208 / 1362394", "0000398549 / MULTIPLE", "  1234/5678", "1362394/TPD"],
)
def test_real_run_8_a_reference_to_another_item_is_a_discount(raw_text: str) -> None:
    assert is_item_reference(raw_text)


@pytest.mark.parametrize(
    "raw_text",
    ["2 @ 2/$10.00", "1/2 PRICE CAKE", "TPD/1362394", "1362394 CHOC PISTACH", "123 / ABC", ""],
)
def test_real_run_8_other_lines_are_not_references(raw_text: str) -> None:
    assert not is_item_reference(raw_text)


# --- real run 2026-11-08 (Superstore): a tax the vision model read where none is printed --------


@pytest.mark.parametrize(
    ("subtotal", "total", "taxes", "tax"),
    [
        (9940, 9940, [899], 0),  # subtotal == total: no tax was charged, whatever was read
        (9940, 9940, [], 0),
        (2555, 2683, [128], 128),
        (2555, 2683, [100, 28], 128),
        (None, 2683, [128], 128),
        (2555, 2683, [], None),
        (None, 2683, [], None),
    ],
)
def test_real_run_tax_is_zero_when_the_subtotal_is_the_total(
    subtotal: int | None, total: int, taxes: list[int], tax: int | None
) -> None:
    assert printed_tax(subtotal, total, taxes) == tax


# --- blind test 2026-11-08 (Superstore): "H=HST 13%    6.49 @ 13.000%     0.84" ----------------


@pytest.mark.parametrize(
    ("text", "cents", "computed_from"),
    [
        ("0.84", 84, None),
        ("0.84 13%", 84, None),  # a rate beside the amount
        ("6.49 @ 13.000% 0.84", 84, None),  # the printed amount after the rate wins
        ("H=HST 13% 6.49 @ 13.000% 0.84", 84, None),  # the whole printed line
        ("6.49 @ 13.000%", 84, "6.49 @ 13.000%"),  # no amount printed: base x rate, half-up
        ("6.49@13%", 84, "6.49 @ 13%"),
        ("14.03 A", 1403, None),
        ("$1.28 GST", 128, None),
        ("6.49 H", 649, None),  # one money value stays as it is; validation judges it
        ("0.84-", -84, None),
        ("GST5% 0.50", 50, None),  # a tax name and rate merged by OCR
        ("HST13.000% 0.84", 84, None),
    ],
)
def test_blind_test_tax_lines_give_the_tax_amount(
    text: str, cents: int, computed_from: str | None
) -> None:
    assert parse_tax(text) == TaxReading(cents=cents, computed_from=computed_from)


@pytest.mark.parametrize("text", ["abc", "", "13%", "6.49 0.84", "HST @ 13%"])
def test_blind_test_anything_else_is_an_error_quoting_the_text(text: str) -> None:
    with pytest.raises(PrintedValueError) as raised:
        parse_tax(text)

    assert f'"{text}"' in str(raised.value)
