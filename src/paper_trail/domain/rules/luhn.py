"""The Luhn checksum that every payment card number satisfies."""

_MIN_DIGITS = 2


def luhn_valid(digits: str) -> bool:
    """True when ``digits`` (ASCII digits only, at least two) passes the Luhn check."""
    if len(digits) < _MIN_DIGITS or not (digits.isascii() and digits.isdigit()):
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:  # noqa: PLR2004 - the sum of the two digits of a doubled digit
                value -= 9
        total += value
    return total % 10 == 0
