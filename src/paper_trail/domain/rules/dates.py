"""Receipt dates, parsed in code: the model's date format is not to be trusted (ADR-0009,
ADR-0017).

The text model returned ``MM/DD/YYYY`` although its schema asked for ISO, and a Superstore
receipt prints ``26/11/02`` (YY/MM/DD), so the date is a string in the model's output and is
read here. Anything unreadable, impossible or in the future is an unknown date, never a failed
extraction.
"""

import re
from collections.abc import Iterator
from datetime import date, timedelta

_TIME = r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)?"
_ISO = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})" + _TIME, re.IGNORECASE)
_SLASHED = re.compile(r"(\d{1,2})([-/])(\d{1,2})\2(\d{4})" + _TIME, re.IGNORECASE)
_SHORT = re.compile(r"(\d{1,2})([-/])(\d{1,2})\2(\d{2})" + _TIME, re.IGNORECASE)
_MONTHS_IN_A_YEAR = 12
_CENTURY = 2000
# A two-digit-year reading at most this old is preferred: receipts are processed within days.
# Below a year on purpose: a YY/MM/DD date printed on day YY-1 ("26/11/25") also reads as D/M/Y
# a year and a day earlier (2025-11-26), 364-365 days old, which must not count as recent.
RECENT_DAYS = 300


def parse_receipt_date(text: str, today: date) -> date | None:
    """The date ``text`` names, or None when it names no date up to ``today``.

    Accepted, each optionally followed by a time (``HH:MM[:SS][ AM|PM]``, ignored):
    ``YYYY-MM-DD`` and ``YYYY/MM/DD``; ``A/B/YYYY`` and ``A-B-YYYY`` read month first, or day
    first when A is above 12; ``A/B/YY`` read month first, then day first, then year first
    (``YY`` is 20YY), taking the first valid reading at most ``RECENT_DAYS`` old, else the
    first valid one up to today. Anything else around the date makes it unreadable.
    """
    value = text.strip()
    if match := _ISO.fullmatch(value):
        year, month, day = (int(part) for part in match.groups())
        return _past(_valid(year, month, day), today)
    if match := _SLASHED.fullmatch(value):
        first, second, year = int(match[1]), int(match[3]), int(match[4])
        month, day = (second, first) if first > _MONTHS_IN_A_YEAR else (first, second)
        return _past(_valid(year, month, day), today)
    if match := _SHORT.fullmatch(value):
        readings = [
            reading
            for reading in _short_year_readings(int(match[1]), int(match[3]), int(match[4]))
            if reading <= today
        ]
        recent = [reading for reading in readings if today - reading <= timedelta(RECENT_DAYS)]
        return next(iter(recent or readings), None)
    return None


def _short_year_readings(a: int, b: int, c: int) -> Iterator[date]:
    """The valid dates ``A/B/YY`` can be, in preference order: M/D/Y, D/M/Y, Y/M/D."""
    for year, month, day in ((c, a, b), (c, b, a), (a, b, c)):
        if (reading := _valid(_CENTURY + year, month, day)) is not None:
            yield reading


def _valid(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _past(reading: date | None, today: date) -> date | None:
    return reading if reading is not None and reading <= today else None
