"""An independent Luhn implementation for checking the production masking (S1-AC-04, S1-AC-21).

Deliberately not ``paper_trail.domain.rules``: a test oracle must not share the code it checks.
"""

import re

# A maximal run of digits, optionally separated by single spaces or dashes (PLAN 3.3).
DIGIT_RUN = re.compile(r"\d(?:[ -]?\d)*")


def luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def with_check_digit(body: str) -> str:
    """``body`` followed by the digit that makes it Luhn-valid."""
    return next(body + digit for digit in "0123456789" if luhn_ok(body + digit))


def card_numbers(text: str) -> list[str]:
    """Every Luhn-valid 13-19 digit run in ``text`` (what masking must never leave behind)."""
    runs = (re.sub(r"[ -]", "", match.group()) for match in DIGIT_RUN.finditer(text))
    return [run for run in runs if 13 <= len(run) <= 19 and luhn_ok(run)]


# LangGraph's checkpoint and task ids are UUIDs. Their dash-separated hex groups can read as a
# Luhn-valid digit run ("e309-6188-8002-59" is 13 digits), but the 8-4-4-4-12 shape of a UUID is
# never a printed card number, so database scans leave UUIDs out.
UUID = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"
)
