"""Is a receipt complete enough to save as COMPLETED? (owner decision 2026-11-08, ADR-0017).

A blind text-path run saved a receipt with every amount right but no date and a misread store,
so it produced no price observation (prices need a date). Pinned, in
``paper_trail.domain.rules.completeness`` (pure): ``check_completeness(store_name, date_text,
purchased_on)`` returns the issues (``IssueCode.MISSING_DATE``, ``IssueCode.MISSING_STORE``)
whose messages are feedback the model can act on; an empty tuple when complete.
"""

from datetime import date

import pytest

from paper_trail.domain.rules.arithmetic import IssueCode
from paper_trail.domain.rules.completeness import check_completeness

DAY = date(2026, 11, 7)


def test_a_store_and_a_date_are_complete() -> None:
    assert check_completeness("REAL CANADIAN SUPERSTORE", "26/11/07", DAY) == ()


def test_no_date_printed_or_read() -> None:
    (issue,) = check_completeness("REAL CANADIAN SUPERSTORE", None, None)

    assert issue.code is IssueCode.MISSING_DATE
    assert issue.message == "no purchase date was read: copy the date exactly as printed"
    assert (issue.expected, issue.actual, issue.position) == (None, None, None)


def test_a_date_that_reads_as_no_date_quotes_it() -> None:
    (issue,) = check_completeness("REAL CANADIAN SUPERSTORE", "12/25/26", None)

    assert issue.code is IssueCode.MISSING_DATE
    assert '"12/25/26"' in issue.message
    assert "copy the date exactly as printed" in issue.message


@pytest.mark.parametrize("store", ["", "   "])
def test_a_blank_store_name(store: str) -> None:
    (issue,) = check_completeness(store, "26/11/07", DAY)

    assert issue.code is IssueCode.MISSING_STORE
    assert issue.message == "no store name was read: copy the store name printed at the top"


def test_both_missing_are_both_reported() -> None:
    codes = [issue.code for issue in check_completeness(" ", None, None)]

    assert codes == [IssueCode.MISSING_STORE, IssueCode.MISSING_DATE]


def test_a_date_over_a_year_old_is_reported_as_a_misread() -> None:
    (issue,) = check_completeness("SUPERSTORE", "2013-11-26", date(2013, 11, 26), DAY)

    assert issue.code is IssueCode.MISSING_DATE
    assert issue.message == (
        'the purchase date "2013-11-26" reads as 2013-11-26, over a year ago: '
        "copy the date exactly as printed"
    )


def test_a_date_within_a_year_is_complete() -> None:
    assert check_completeness("SUPERSTORE", "25/11/08", date(2025, 11, 8), DAY) == ()


@pytest.mark.parametrize(
    ("purchased_on", "aged", "codes"),
    [
        (date(2026, 11, 8), True, [IssueCode.MISSING_DATE]),
        (date(2026, 11, 8), False, [IssueCode.MISSING_DATE]),
        (date(2026, 11, 7), True, []),
        (date(2024, 1, 15), True, [IssueCode.MISSING_DATE]),
        (date(2024, 1, 15), False, []),
    ],
    ids=["future", "future-not-aged", "today", "over-a-year", "over-a-year-not-aged"],
)
def test_a_date_after_today_is_never_complete_and_an_old_one_only_where_age_counts(
    purchased_on: date, aged: bool, codes: list[IssueCode]
) -> None:
    issues = check_completeness("Costco", purchased_on.isoformat(), purchased_on, DAY, aged=aged)

    assert [issue.code for issue in issues] == codes
