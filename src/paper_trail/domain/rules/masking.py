"""Masking payment card numbers before anything is stored or logged (ADR-0011)."""

import re
from dataclasses import dataclass

from paper_trail.domain.models import JsonValue
from paper_trail.domain.rules.luhn import luhn_valid

MASK = "*"
KEPT_DIGITS = 4
_MIN_PAN_DIGITS = 13
_MAX_PAN_DIGITS = 19

# A maximal run of digits, optionally separated by single spaces or dashes.
_CANDIDATE = re.compile(r"[0-9](?:[ -]?[0-9])*")


@dataclass(frozen=True, slots=True)
class MaskResult:
    text: str
    masked_count: int


def mask_pans(text: str) -> MaskResult:
    """Replace every digit of each card number in ``text`` but its last four with ``*``.

    A candidate is a maximal run of 13 to 19 digits, optionally separated by single spaces or
    dashes, that passes the Luhn check. Separators and everything else are kept, so the text
    keeps its length and layout. Masking masked text changes nothing.
    """
    pieces: list[str] = []
    masked = 0
    end = 0
    for match in _CANDIDATE.finditer(text):
        run = match.group()
        digits = "".join(char for char in run if char.isdigit())
        if _MIN_PAN_DIGITS <= len(digits) <= _MAX_PAN_DIGITS and luhn_valid(digits):
            pieces += [text[end : match.start()], _mask_run(run, len(digits))]
            end = match.end()
            masked += 1
    pieces.append(text[end:])
    return MaskResult(text="".join(pieces), masked_count=masked)


def _mask_run(run: str, digit_count: int) -> str:
    hidden = digit_count - KEPT_DIGITS
    masked: list[str] = []
    for char in run:
        if char.isdigit() and hidden > 0:
            masked.append(MASK)
            hidden -= 1
        else:
            masked.append(char)
    return "".join(masked)


def mask_json(value: JsonValue) -> JsonValue:
    """``value`` with card numbers masked in every string it contains, keys excepted."""
    if isinstance(value, str):
        return mask_pans(value).text
    if isinstance(value, list):
        return [mask_json(item) for item in value]
    if isinstance(value, dict):
        return {key: mask_json(item) for key, item in value.items()}
    return value
