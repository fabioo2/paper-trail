"""Values as printed on a receipt, turned into numbers in code (ADR-0017).

The extraction model copies amounts, quantities and unit prices exactly as printed ("4.00-",
"0.460 kg", "2/$10.00"); these parsers compute the signed integer cents and ``Decimal``
quantities. A model that never computes cannot write 1.29 as 1290 cents or drop a minus sign.
Every error quotes the printed text, so it can be sent back to the model as feedback.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from paper_trail.domain.errors import PrintedValueError

# An amount with exactly two fraction digits: optional parentheses, a leading minus (ASCII or
# U+2212), "$", thousands commas, a "." or "," decimal mark, then a trailing "-" or "CR" and
# 1-4 tax-flag letters ("A", "H", "GST", "MRJ"), which are ignored.
_AMOUNT = re.compile(
    r"""
    (?P<open>\()?\s*
    (?P<lead>[-\N{MINUS SIGN}])?\s*
    \$?\s*
    (?P<units>\d{1,3}(?:,\d{3})+|\d+)
    [.,](?P<cents>\d{2})
    \s*(?P<close>\))?
    \s*(?P<trail>-|CR)?
    (?:\s*[A-Z]{1,4})?
    """,
    re.VERBOSE | re.IGNORECASE,
)
# A quantity: a positive number with an optional unit or multiplication marker around it,
# optionally followed by the rest of its detail line ("0.225 kg @ $7.69/kg", "2 @ 2/$10.00").
_QUANTITY = re.compile(
    r"(?:[x@]\s*)?(?P<number>\d+(?:[.,]\d+)?)\s*(?:kg|lb|ea|x|@)?\s*(?:[x@].*)?",
    re.IGNORECASE | re.DOTALL,
)
# A multi-buy price: "N/$X" and "N for $X" are N units for X; a quantity bought may come first
# ("2 @ 2/$10.00").
_MULTI_BUY = re.compile(
    r"(?:\d+\s+)?(?P<count>\d+)\s*(?:/|for)\s*(?P<price>\$?\s*\d+[.,]\d{2})", re.IGNORECASE
)
# What may surround a unit price: "@", a unit suffix ("/kg", "/lb", "ea", "/each").
_UNIT_PRICE_NOISE = re.compile(r"@|/?\s*\b(?:kg|lb|ea|each)\b\.?", re.IGNORECASE)
# An amount printed without its units digit (".99"): it is 0.99.
_NO_UNITS = re.compile(r"(?<![\d.,])([.,]\d{2})\b")
_AMOUNT_EXAMPLE = 'write the amount exactly as printed, with its cents, e.g. "12.90" or "4.00-"'


def parse_amount(text: str) -> int:
    """Signed integer cents of an amount as printed; ``PrintedValueError`` otherwise.

    Negative for a leading minus, a trailing minus, a trailing ``CR`` or parentheses.
    """
    match = _AMOUNT.fullmatch(text.strip())
    if match is None or bool(match["open"]) != bool(match["close"]):
        msg = f'"{text}" is not an amount: {_AMOUNT_EXAMPLE}'
        raise PrintedValueError(msg)
    cents = int(match["units"].replace(",", "")) * 100 + int(match["cents"])
    negative = match["lead"] or match["trail"] or match["open"]
    return -cents if negative else cents


def parse_quantity(text: str | None) -> Decimal:
    """The positive quantity printed ("2", "0.460 kg", "2 @"); 1 when none is printed."""
    if text is None or not text.strip():
        return Decimal(1)
    match = _QUANTITY.fullmatch(text.strip())
    quantity = Decimal(match["number"].replace(",", ".")) if match else Decimal(0)
    if quantity <= 0:
        msg = f'"{text}" is not a quantity: write the count or weight as printed, e.g. "2"'
        raise PrintedValueError(msg)
    return quantity


def parse_unit_price(text: str | None) -> int | None:
    """Cents of one unit (or one kg/lb), or None when unreadable: the unit price is optional
    and only feeds the line check. ``N/$X`` is X/N, rounded half-up."""
    exact = parse_unit_price_exact(text)
    if exact is None:
        return None
    return int((Decimal(exact.numerator) / exact.denominator).quantize(Decimal(1), ROUND_HALF_UP))


def parse_unit_price_exact(text: str | None) -> Fraction | None:
    """Cents of one unit as an exact fraction: ``N/$X`` and ``N for $X`` are X/N (``"3/$10.00"``
    is 1000/3), any other price its printed cents; a unit suffix (``"/lb"``, ``"ea"``) is
    ignored and ``".99"`` is 0.99. None when unreadable. The line check multiplies this, so a
    deal price never loses the cent its rounding would."""
    if text is None:
        return None
    price = _NO_UNITS.sub(r"0\1", _UNIT_PRICE_NOISE.sub(" ", text)).strip()
    multi_buy = _MULTI_BUY.fullmatch(price)
    if multi_buy and int(multi_buy["count"]) > 0:
        return Fraction(parse_amount(multi_buy["price"]), int(multi_buy["count"]))
    cents = _cents_or_none(price)
    return None if cents is None else Fraction(cents)


def _cents_or_none(text: str) -> int | None:
    try:
        return parse_amount(text)
    except PrintedValueError:
        return None


@dataclass(frozen=True, slots=True)
class TaxReading:
    """A tax line's amount; ``computed_from`` ("6.49 @ 13.000%") when no amount was printed
    and it is the taxable base times the rate, so that the trace can say so."""

    cents: int
    computed_from: str | None = None


# A rate ("13%", "13.000 %") and an amount alone, as tokens of a tax line.
_RATE = re.compile(r"(?P<rate>\d+(?:[.,]\d+)?)%")
_MONEY = re.compile(r"[-\N{MINUS SIGN}(]?\$?\d[\d,]*[.,]\d{2}(?:-|\))?")
# Tokens that are neither a tax name nor a number but belong to an amount.
_AMOUNT_MARKS = frozenset({"$", "-", "CR", "(", ")"})


def parse_tax(text: str) -> TaxReading:
    """The tax amount of a tax line copied in part or whole ("H=HST 13% 6.49 @ 13.000% 0.84").

    Rates and tax names are dropped. "base @ rate%" uses the amount printed after the rate, or
    else base x rate rounded half-up; otherwise the one amount left is the tax (signed as in
    ``parse_amount``). Anything else is a ``PrintedValueError`` quoting the text.
    """
    spaced = re.sub(r"(\d)\s+%", r"\1%", text).replace("@", " @ ")
    tokens = spaced.split()
    if "@" in tokens:
        at = tokens.index("@")
        before, after = tokens[:at], tokens[at + 1 :]
        rate = _RATE.fullmatch(after[0]) if after else None
        if before and rate and _MONEY.fullmatch(before[-1]):
            printed = [token for token in after[1:] if _MONEY.fullmatch(token)]
            if printed:
                return TaxReading(_tax_amount(text, " ".join(printed)))
            base = Decimal(parse_amount(before[-1]))
            rate_value = Decimal(rate["rate"].replace(",", "."))
            cents = (base * rate_value / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            return TaxReading(int(cents), f"{before[-1]} @ {after[0]}")
        return TaxReading(_tax_amount(text, ""))
    kept = [
        token
        for token in tokens
        if not token.endswith("%")  # a rate, alone or merged into a name ("GST5%")
        and (any(char.isdigit() for char in token) or token.upper() in _AMOUNT_MARKS)
    ]
    return TaxReading(_tax_amount(text, " ".join(kept)))


def _tax_amount(text: str, amount: str) -> int:
    try:
        return parse_amount(amount)
    except PrintedValueError:
        msg = f'"{text}" is not a tax amount: write the amount at the right of the tax line'
        raise PrintedValueError(msg) from None


def printed_tax(subtotal_cents: int | None, total_cents: int, taxes: Sequence[int]) -> int | None:
    """The receipt's tax: 0 when the printed subtotal is the printed total (no tax was
    charged, whatever tax a model read), else the sum of the tax lines, None without any."""
    if subtotal_cents == total_cents:
        return 0
    return sum(taxes) if taxes else None
