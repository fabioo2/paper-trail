"""Receipt dates are parsed in code, never trusted to the model (PLAN 5, ADR-0009 amendment).

The text model returned MM/DD/YYYY although its schema asked for ISO (measured 2026-11-06), so
``ExtractionOutput.purchased_on`` is a string and ``parse_receipt_date(text, today)`` decides.

Pinned (docs/tests/agent-core-A.md): surrounding whitespace is ignored; words around the date
make it unparseable; two-digit years are 20YY; an impossible or future date is None (stored as
"unknown date", never an extraction failure).

Extraction accuracy (docs/tests/extraction-accuracy.md, EA-AC-06): a trailing time
(``HH:MM[:SS][ AM|PM]``) is ignored. For ``A/B/YY`` the readings are tried month first
(M/D/Y), then day first (D/M/Y), then year first (Y/M/D); the first valid one that is not in
the future and at most 300 days old wins, else the first valid one that is not in the future.
Fix round: a 366-day window let the day-first reading of a YY/MM/DD date printed on day YY-1
win, being only 364-365 days old.
"""

from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paper_trail.domain.rules.dates import parse_receipt_date

TODAY = date(2026, 11, 6)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-01", date(2026, 11, 1)),
        ("2026/11/01", date(2026, 11, 1)),
        ("  2026-11-01\n", date(2026, 11, 1)),
        ("11/01/2026", date(2026, 11, 1)),  # what the model actually returned: month first
        ("11-01-2026", date(2026, 11, 1)),
        ("3/4/2026", date(2026, 3, 4)),
        ("03/04/26", date(2026, 3, 4)),
        ("25/10/2026", date(2026, 10, 25)),  # first part above 12: day first
        ("25/10/26", date(2026, 10, 25)),
        ("12/31/2025", date(2025, 12, 31)),
        ("2026-11-06", TODAY),  # today is not the future
        ("11/01/2026 14:32", date(2026, 11, 1)),  # EA-AC-06: a trailing time is ignored
    ],
)
def test_parses_iso_and_slashed_dates(text: str, expected: date) -> None:
    """PLAN 5: ISO first; A/B/YYYY and A/B/YY are month/day unless the first part is > 12."""
    assert parse_receipt_date(text, TODAY) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "unknown",
        "2026-02-30",  # impossible
        "13/13/2026",  # impossible either way
        "02/30/2026",
        "2026-11-07",  # tomorrow
        "12/01/2026",  # December 1st, in the future
        "Nov 1 2026",
        "2026-11",
        "1/2/3/2026",
        "11/01/202",
    ],
)
def test_unparseable_impossible_or_future_dates_are_none(text: str) -> None:
    """PLAN 5: the receipt is stored with an unknown date rather than failing extraction."""
    assert parse_receipt_date(text, TODAY) is None


@given(st.dates(min_value=date(2000, 1, 1), max_value=TODAY))
def test_iso_round_trips_for_every_past_date(value: date) -> None:
    """Property: an ISO date up to today parses to itself."""
    assert parse_receipt_date(value.isoformat(), TODAY) == value


@given(st.dates(min_value=date(2000, 1, 1), max_value=TODAY))
def test_month_first_reading_for_every_past_date(value: date) -> None:
    """Property: MM/DD/YYYY parses to that date (its first part is a month, never above 12)."""
    assert parse_receipt_date(value.strftime("%m/%d/%Y"), TODAY) == value


@given(st.dates(min_value=TODAY + timedelta(days=1), max_value=date(2099, 12, 31)))
def test_future_dates_are_never_returned(value: date) -> None:
    """Property: no reading of a future date is accepted."""
    assert parse_receipt_date(value.isoformat(), TODAY) is None


# --- EA-AC-06: two-digit years prefer a recent reading; a trailing time is ignored --------------

EA_TODAY = date(2026, 11, 8)  # the day of the real runs (docs/plans/extraction-accuracy.md)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("26/11/02", date(2026, 11, 2)),  # Superstore: YY/MM/DD (was read as 2002-11-26)
        ("11/02/26", date(2026, 11, 2)),  # month first
        ("02/11/26", date(2026, 2, 11)),  # month first still wins when it is recent
        ("2026/11/05", date(2026, 11, 5)),  # Costco: four-digit year first
        ("26/11/02 14:31", date(2026, 11, 2)),
        ("26/11/02 14:31:09", date(2026, 11, 2)),
        ("11/02/26 2:31 PM", date(2026, 11, 2)),
        ("2026/11/05 18:02", date(2026, 11, 5)),
        ("25/10/26", date(2026, 10, 25)),  # day first when the first part is above 12
        ("11/08/26", EA_TODAY),  # today is not the future
        ("01/02/03", date(2003, 1, 2)),  # nothing recent: the first valid past reading
    ],
)
def test_ea_ac_06_two_digit_years_prefer_a_recent_past_reading(text: str, expected: date) -> None:
    assert parse_receipt_date(text, EA_TODAY) == expected


@pytest.mark.parametrize(
    ("text", "today", "expected"),
    [
        # YY/MM/DD printed on day YY-1: the D/M/Y reading is a year old, not recent.
        ("26/11/25", date(2026, 11, 25), date(2026, 11, 25)),
        ("26/12/25", date(2026, 12, 26), date(2026, 12, 25)),
    ],
)
def test_review_1_a_year_old_day_first_reading_does_not_win(
    text: str, today: date, expected: date
) -> None:
    assert parse_receipt_date(text, today) == expected


@pytest.mark.parametrize(
    "text",
    [
        "12/25/26",  # Christmas 2026: every valid reading is in the future
        "12/31/26",  # New Year's Eve 2026: no reading is in the past
        "26/11/02 noon",  # words after the date
    ],
)
def test_ea_ac_06_future_only_or_unreadable_dates_are_none(text: str) -> None:
    assert parse_receipt_date(text, EA_TODAY) is None
