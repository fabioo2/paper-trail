"""Reconciling a multi-buy the vision model missed, from the OCR text (real run 2026-11-08).

The Superstore photo prints two different products, an offer line and the group's amount:

    (1)05700001285 CRV CHKN PBLN BR MRJ
    (1)05700001299 CRV CRN SD BRRT MRJ
    $5.99 ea or 2/$10.00 KB
    2 @ 2/$10.00                10.00

The vision model gave both products the offer's 5.99, so the items exceed the subtotal by
2 x 5.99 - 10.00 = 1.98. Tesseract read the "2 @ 2/$10.00 10.00" line. Pinned, in
``paper_trail.domain.rules.reconcile`` (pure):
``reconcile_multi_buy(lines, subtotal_cents, ocr_text) -> MultiBuy | None``, ``lines`` being
(kind, line total cents) pairs. Exact or nothing: one OCR multi-buy line "N @ N/$X  X", one run
of N adjacent ITEM lines with equal totals p, and items - subtotal == N*p - X; the run's new
totals split X like ``share_amounts`` (remainder on the last line). Anything else is None.
"""

from collections.abc import Sequence

import pytest

from paper_trail.domain.enums import LineKind
from paper_trail.domain.rules.reconcile import MultiBuy, reconcile_multi_buy

ITEM, DISCOUNT = LineKind.ITEM, LineKind.DISCOUNT
OCR = """REAL CANADIAN SUPERSTORE
(1)05700001285 CRV CHKN PBLN BR MRJ
(1)05700001299 CRV CRN SD BRRT MRJ
$5.99 ea or 2/$10.00 KB
2 @ 2/$10.00                10.00
BANANAS                      1.52
SUBTOTAL                    99.40
"""
OTHERS = [(ITEM, 8788), (ITEM, 152)]  # the rest of the receipt: 87.88 + 1.52 = 89.40
SUPERSTORE = [*OTHERS[:1], (ITEM, 599), (ITEM, 599), *OTHERS[1:]]
SUBTOTAL = 9940  # 89.40 + 10.00


def _sum(lines: Sequence[tuple[LineKind, int]]) -> int:
    return sum(cents for _, cents in lines)


def test_the_superstore_multi_buy_is_reconciled_to_the_subtotal() -> None:
    assert _sum(SUPERSTORE) - SUBTOTAL == 198

    fix = reconcile_multi_buy(SUPERSTORE, SUBTOTAL, OCR)

    assert fix == MultiBuy(detail="2 @ 2/$10.00", first=1, shares=(500, 500))
    assert _sum(OTHERS) + sum(fix.shares) == SUBTOTAL


def test_an_odd_group_price_puts_the_remainder_on_the_last_line() -> None:
    lines = [(ITEM, 599), (ITEM, 599)]

    fix = reconcile_multi_buy(lines, 1001, "2 @ 2/$10.01   10.01")

    assert fix is not None
    assert fix.shares == (500, 501)


@pytest.mark.parametrize(
    ("lines", "subtotal", "ocr"),
    [
        (SUPERSTORE, SUBTOTAL, OCR.replace("2 @ 2/$10.00                10.00\n", "")),
        (SUPERSTORE, SUBTOTAL, OCR.replace("10.00\nBANANAS", "9.00\nBANANAS")),  # X != amount
        ([(ITEM, 599), (ITEM, 599), (ITEM, 599)], 1599, OCR),  # two candidate runs
        (SUPERSTORE, SUBTOTAL + 1, OCR),  # the result would not be exact
        ([(ITEM, 8788), (ITEM, 599), (ITEM, 600), (ITEM, 152)], 9940 + 1, OCR),  # prices differ
        ([(ITEM, 8788), (ITEM, 599), (DISCOUNT, 599), (ITEM, 152)], SUBTOTAL, OCR),  # not items
        (SUPERSTORE, None, OCR),  # no subtotal printed
        (SUPERSTORE, _sum(SUPERSTORE), OCR),  # already adds up
    ],
    ids=[
        "no-ocr-line",
        "group-price-not-the-amount",
        "two-runs",
        "not-exact",
        "prices-differ",
        "not-items",
        "no-subtotal",
        "adds-up",
    ],
)
def test_anything_but_one_exact_fix_changes_nothing(
    lines: list[tuple[LineKind, int]], subtotal: int | None, ocr: str
) -> None:
    assert reconcile_multi_buy(lines, subtotal, ocr) is None


# --- Tesseract misreads of the multi-buy line (review, low) --------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "2 @ 2/$10.00   10.00",
        "2 @ 2/S10.00   10.00",  # "$" read as "S"
        "2 @ 2 / $10.00   10.00",
        "2@2/10.00 10.00",
        "2 @ 2/ $ 10.00   10.00",
    ],
)
def test_misread_multi_buy_lines_still_reconcile(line: str) -> None:
    fix = reconcile_multi_buy(
        SUPERSTORE, SUBTOTAL, OCR.replace("2 @ 2/$10.00                10.00", line)
    )

    assert fix is not None
    assert fix.shares == (500, 500)


@pytest.mark.parametrize(
    "line",
    [
        "12 @ 2/$10.00   10.00",  # the count is 12, not the "2" at its end
        "2 @ 3/$10.00   10.00",  # the group size is not the count
    ],
)
def test_lines_that_are_not_this_multi_buy_do_not_match(line: str) -> None:
    ocr = OCR.replace("2 @ 2/$10.00                10.00", line)

    assert reconcile_multi_buy(SUPERSTORE, SUBTOTAL, ocr) is None
