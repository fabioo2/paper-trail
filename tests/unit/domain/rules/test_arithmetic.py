"""Receipt arithmetic validation (S1-AC-05).

Pinned (docs/tests/stage1.md): ``expected`` is the value computed from other fields and
``actual`` the value printed on the receipt; messages number lines from 1 ("line 3" is
position 2) and print amounts as plain decimals ("9.98", "-1.00").
"""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paper_trail.domain.enums import LineKind
from paper_trail.domain.models import LineItem, ReceiptDraft
from paper_trail.domain.money import Money
from paper_trail.domain.rules.arithmetic import (
    ArithmeticIssue,
    ArithmeticReport,
    IssueCode,
    Tolerances,
    check_arithmetic,
)
from tests.factories import cad, draft_with, line_item, receipt, receipt_draft


def _item(position: int, total: int, *, unit: int | None = None, qty: str = "1") -> LineItem:
    return line_item(
        position=position,
        quantity=Decimal(qty),
        unit_price=None if unit is None else cad(unit),
        line_total=cad(total),
    )


def _codes(report: ArithmeticReport) -> list[IssueCode]:
    return [issue.code for issue in report.issues]


def _amount(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def test_s1_ac_05_default_draft_is_consistent() -> None:
    """S1-AC-05: a receipt with subtotal and tax that adds up is ok."""
    report = check_arithmetic(receipt_draft())

    assert report.ok
    assert report.issues == ()


def test_s1_ac_05_consistent_without_subtotal() -> None:
    """S1-AC-05 rule 4: without a subtotal, items + tax must equal the total."""
    draft = draft_with([_item(0, 899, unit=899), _item(1, 658, unit=329, qty="2")],
                       subtotal=None, tax=78, total=1635)  # fmt: skip

    assert check_arithmetic(draft).ok


def test_s1_ac_05_consistent_without_subtotal_or_tax() -> None:
    """S1-AC-05 rule 4: a missing tax counts as zero."""
    draft = draft_with([_item(0, 899)], subtotal=None, tax=None, total=899)

    assert check_arithmetic(draft).ok


def test_s1_ac_05_consistent_with_discount_and_deposit_lines() -> None:
    """S1-AC-05: DISCOUNT lines carry negative totals and are summed like any other line."""
    items = [
        _item(0, 1299, unit=1299),
        line_item(position=1, kind=LineKind.DISCOUNT, raw_text="TPD/1234567",
                  description="Instant savings", unit_price=None, line_total=cad(-300)),
        line_item(position=2, kind=LineKind.DEPOSIT, raw_text="DEPOSIT",
                  description="Bottle deposit", unit_price=cad(10), quantity=Decimal("12"),
                  line_total=cad(120)),
        line_item(position=3, kind=LineKind.FEE, raw_text="ECO FEE",
                  description="Environmental fee", unit_price=None, line_total=cad(25)),
    ]  # fmt: skip
    draft = draft_with(items, subtotal=1144, tax=149, total=1293)

    assert check_arithmetic(draft).ok


def test_s1_ac_05_receipt_entities_are_checked_too() -> None:
    """S1-AC-05: check_arithmetic takes anything ReceiptLike, including a stored Receipt."""
    assert check_arithmetic(receipt()).ok


def test_s1_ac_05_a_receipt_still_processing_has_no_total_to_check() -> None:
    """Without a total (still processing) rules 3 and 4 are skipped; the rest still apply."""
    assert check_arithmetic(receipt(total=None)).ok
    assert [issue.code for issue in check_arithmetic(receipt(total=None, subtotal=cad(1)))
            .issues] == [IssueCode.ITEMS_VS_SUBTOTAL]  # fmt: skip


def test_s1_ac_05_tolerates_a_one_cent_line_rounding_difference() -> None:
    """S1-AC-05: 3 x 3.33 = 9.99 printed as 10.00 is within the 1-cent per-line tolerance."""
    draft = draft_with([_item(0, 1000, unit=333, qty="3")], subtotal=1000, tax=None, total=1000)

    assert check_arithmetic(draft).ok


def test_s1_ac_05_weighed_line_rounds_half_up_before_comparing() -> None:
    """S1-AC-05: 0.452 kg x 10.99 = 4.96748 -> 4.97 matches a printed 4.97 exactly."""
    draft = draft_with([_item(0, 497, unit=1099, qty="0.452")], subtotal=None, tax=None, total=497)

    assert check_arithmetic(draft).ok


def test_s1_ac_05_wrong_line_math_beyond_one_cent() -> None:
    """S1-AC-05: line 3 printed 2 x 4.99 = 9.89 is LINE_MATH, naming the line and both amounts."""
    items = [_item(0, 100, unit=100), _item(1, 200, unit=200), _item(2, 989, unit=499, qty="2")]
    draft = draft_with(items, subtotal=1289, tax=None, total=1289)

    report = check_arithmetic(draft)

    assert not report.ok
    (issue,) = report.issues
    assert issue.code is IssueCode.LINE_MATH
    assert issue.position == 2
    assert issue.expected == cad(998)
    assert issue.actual == cad(989)
    assert "line 3" in issue.message
    assert "9.98" in issue.message
    assert "9.89" in issue.message


def test_s1_ac_05_two_cents_off_is_beyond_the_line_tolerance() -> None:
    """S1-AC-05: the per-line tolerance is exactly 1 cent."""
    draft = draft_with([_item(0, 1001, unit=333, qty="3")], subtotal=1001, tax=None, total=1001)

    assert _codes(check_arithmetic(draft)) == [IssueCode.LINE_MATH]


def test_s1_ac_05_items_not_summing_to_subtotal() -> None:
    """S1-AC-05 rule 2: ITEMS_VS_SUBTOTAL with the sum as expected and the subtotal as actual."""
    draft = draft_with([_item(0, 899), _item(1, 658)], subtotal=1600, tax=None, total=1600)

    (issue,) = check_arithmetic(draft).issues

    assert issue.code is IssueCode.ITEMS_VS_SUBTOTAL
    assert (issue.expected, issue.actual, issue.position) == (cad(1557), cad(1600), None)
    assert "15.57" in issue.message
    assert "16.00" in issue.message


def test_s1_ac_05_subtotal_plus_tax_not_equal_total() -> None:
    """S1-AC-05 rule 3: SUBTOTAL_PLUS_TAX_VS_TOTAL."""
    draft = draft_with([_item(0, 1557)], subtotal=1557, tax=78, total=1653)

    (issue,) = check_arithmetic(draft).issues

    assert issue.code is IssueCode.SUBTOTAL_PLUS_TAX_VS_TOTAL
    assert (issue.expected, issue.actual) == (cad(1635), cad(1653))
    assert "16.35" in issue.message
    assert "16.53" in issue.message


def test_s1_ac_05_subtotal_without_tax_must_equal_total() -> None:
    """Pinned: with a subtotal and no tax, the tax counts as zero in rule 3 (as in rule 4)."""
    draft = draft_with([_item(0, 1557)], subtotal=1557, tax=None, total=1600)

    assert _codes(check_arithmetic(draft)) == [IssueCode.SUBTOTAL_PLUS_TAX_VS_TOTAL]


def test_s1_ac_05_items_plus_tax_not_equal_total() -> None:
    """S1-AC-05 rule 4: ITEMS_PLUS_TAX_VS_TOTAL when there is no subtotal."""
    draft = draft_with([_item(0, 899), _item(1, 658)], subtotal=None, tax=78, total=1700)

    (issue,) = check_arithmetic(draft).issues

    assert issue.code is IssueCode.ITEMS_PLUS_TAX_VS_TOTAL
    assert (issue.expected, issue.actual) == (cad(1635), cad(1700))


def test_s1_ac_05_totals_have_no_tolerance_by_default() -> None:
    """S1-AC-05: Tolerances() is 1 cent per line and 0 cents on totals."""
    assert Tolerances() == Tolerances(per_line_cents=1, total_cents=0)
    draft = draft_with([_item(0, 1557)], subtotal=1557, tax=78, total=1636)

    assert _codes(check_arithmetic(draft)) == [IssueCode.SUBTOTAL_PLUS_TAX_VS_TOTAL]
    assert check_arithmetic(draft, Tolerances(total_cents=1)).ok


@pytest.mark.parametrize("kind", [LineKind.ITEM, LineKind.DEPOSIT, LineKind.FEE])
def test_s1_ac_05_negative_non_discount_line(kind: LineKind) -> None:
    """S1-AC-05 rule 5: only DISCOUNT lines may be negative."""
    items = [
        _item(0, 1000),
        line_item(position=1, kind=kind, unit_price=None, line_total=cad(-100)),
    ]
    draft = draft_with(items, subtotal=900, tax=None, total=900)

    (issue,) = check_arithmetic(draft).issues

    assert issue.code is IssueCode.NEGATIVE_ITEM
    assert issue.position == 1
    assert issue.actual == cad(-100)
    assert "line 2" in issue.message
    assert "-1.00" in issue.message


def test_s1_ac_05_mixed_currency_is_reported_not_raised() -> None:
    """S1-AC-05 rule 6 (pinned): only CURRENCY_MISMATCH; the sums are skipped, nothing raises."""
    items = [_item(0, 899), line_item(position=1, unit_price=None, line_total=Money(658, "USD"))]
    draft = draft_with(items, subtotal=1557, tax=78, total=1635)

    report = check_arithmetic(draft)

    assert set(_codes(report)) == {IssueCode.CURRENCY_MISMATCH}
    (issue,) = report.issues
    assert issue.position == 1
    assert issue.actual == Money(658, "USD")
    assert "line 2" in issue.message
    assert "USD" in issue.message


def test_s1_ac_05_mixed_currency_in_totals() -> None:
    """S1-AC-05 rule 6: receipt-level amounts in another currency are mismatches too."""
    draft = receipt_draft(
        items=(_item(0, 1557),), subtotal=cad(1557), tax=Money(78, "USD"), total=cad(1635)
    )

    (issue,) = check_arithmetic(draft).issues

    assert issue.code is IssueCode.CURRENCY_MISMATCH
    assert issue.position is None


def test_s1_ac_05_empty_receipt() -> None:
    """S1-AC-05 rule 7: a receipt needs at least one line."""
    draft = draft_with([], subtotal=None, tax=None, total=0)

    (issue,) = check_arithmetic(draft).issues

    assert issue.code is IssueCode.EMPTY_RECEIPT
    assert (issue.expected, issue.actual, issue.position) == (None, None, None)
    assert issue.message


def test_s1_ac_05_reports_every_problem_in_one_pass() -> None:
    """S1-AC-05: independent problems are all reported (line math and subtotal and total)."""
    items = [_item(0, 989, unit=499, qty="2"), _item(1, 500)]
    draft = draft_with(items, subtotal=1600, tax=78, total=1700)

    assert sorted(_codes(check_arithmetic(draft))) == sorted(
        [IssueCode.LINE_MATH, IssueCode.ITEMS_VS_SUBTOTAL, IssueCode.SUBTOTAL_PLUS_TAX_VS_TOTAL]
    )


def test_s1_ac_05_every_message_names_the_line_and_both_amounts() -> None:
    """S1-AC-05: each issue's message names its line (when it has one) and its amounts."""
    items = [_item(0, 989, unit=499, qty="2"), _item(1, -50), _item(2, 500)]
    draft = draft_with(items, subtotal=1600, tax=78, total=1700)

    issues: tuple[ArithmeticIssue, ...] = check_arithmetic(draft).issues

    assert len(issues) >= 3
    for issue in issues:
        if issue.position is not None:
            assert f"line {issue.position + 1}" in issue.message, issue
        for amount in (issue.expected, issue.actual):
            if amount is not None:
                assert _amount(amount.amount_cents) in issue.message, issue


@st.composite
def _consistent_draft(draw: st.DrawFn) -> ReceiptDraft:
    lines: list[LineItem] = []
    for position in range(draw(st.integers(min_value=1, max_value=8))):
        unit = draw(st.integers(min_value=1, max_value=50_000))
        qty = draw(st.decimals(min_value=Decimal("0.001"), max_value=20, places=3))
        lines.append(_item(position, cad(unit).times(qty).amount_cents, unit=unit, qty=str(qty)))
    discount = draw(st.integers(min_value=0, max_value=500))
    if discount:
        lines.append(line_item(position=len(lines), kind=LineKind.DISCOUNT, unit_price=None,
                               line_total=cad(-discount)))  # fmt: skip
    items_total = sum(line.line_total.amount_cents for line in lines)
    tax = draw(st.integers(min_value=0, max_value=5_000))
    subtotal = items_total if draw(st.booleans()) else None
    return draft_with(lines, subtotal=subtotal, tax=tax, total=items_total + tax)


@given(draft=_consistent_draft())
def test_s1_ac_05_property_consistent_receipts_are_ok(draft: ReceiptDraft) -> None:
    """Property: lines priced with Money.times and totals that add up always pass."""
    report = check_arithmetic(draft)

    assert report.ok, report.issues
