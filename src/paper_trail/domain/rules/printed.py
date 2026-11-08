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

from paper_trail.domain.enums import LineKind
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
# A line that refers to another item's code: Costco's instant savings ("0000392208 / 1362394",
# "0000398549 / MULTIPLE"), a discount even when its minus sign was not read.
_ITEM_REFERENCE = re.compile(r"\s*\d{4,}\s*/\s*\S+")
# A multi-buy price: "N/$X" is N units for X.
_MULTI_BUY = re.compile(r"(?P<count>\d+)\s*/\s*(?P<price>\$?\s*\d+[.,]\d{2})")
# What may surround a unit price: "@", "ea", "/kg", "/lb".
_UNIT_PRICE_NOISE = re.compile(r"@|/\s*(?:kg|lb)\b|\bea\b", re.IGNORECASE)
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
    if text is None:
        return None
    multi_buy = _MULTI_BUY.fullmatch(text.strip())
    if multi_buy and int(multi_buy["count"]) > 0:
        price = Decimal(parse_amount(multi_buy["price"]))
        return int((price / int(multi_buy["count"])).quantize(Decimal(1), ROUND_HALF_UP))
    return _cents_or_none(_UNIT_PRICE_NOISE.sub(" ", text))


def _cents_or_none(text: str) -> int | None:
    try:
        return parse_amount(text)
    except PrintedValueError:
        return None


def is_item_reference(raw_text: str) -> bool:
    """Whether the line refers to another item's code (``"0000392208 / 1362394"``): such a line
    is a saving on that item, so a DISCOUNT."""
    return _ITEM_REFERENCE.match(raw_text) is not None


def line_quantity(quantity: Decimal, unit_price_cents: int | None, cents: int | None) -> Decimal:
    """The line's quantity: 1 when the printed unit price is the printed amount, whatever
    quantity was read (a size or count in the name, "KIMCHI 1.5KG", is not a quantity)."""
    if unit_price_cents is not None and unit_price_cents == cents:
        return Decimal(1)
    return quantity


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


def line_kind(kind: LineKind, cents: int) -> tuple[LineKind, int]:
    """The line's kind and signed total: a negative line or a discount is a negative DISCOUNT;
    anything else stays as given."""
    if cents < 0 or kind is LineKind.DISCOUNT:
        return LineKind.DISCOUNT, -abs(cents)
    return kind, cents


@dataclass(frozen=True, slots=True)
class LineAmount:
    """A line's parsed numbers; ``cents`` is None when the line has no amount printed."""

    quantity: Decimal
    unit_price_cents: int | None
    cents: int | None


@dataclass(frozen=True, slots=True)
class PricedLine:
    """A line's numbers once every line has its amount."""

    quantity: Decimal
    unit_price_cents: int | None
    cents: int


def share_amounts(lines: Sequence[LineAmount]) -> list[PricedLine]:
    """Every line with an amount: a multi-buy whose one printed amount covers several products.

    A run of k >= 1 lines without an amount, followed by a priced line whose quantity is k+1,
    becomes k+1 lines of quantity 1 and no unit price, the amount split evenly (the remainder
    cents on the last line). Any other missing amount is a ``PrintedValueError`` naming the
    line (1-based): the group-size guard stops a model from hiding an unreadable price.
    """
    priced: list[PricedLine] = []
    problems: list[str] = []
    run = 0  # lines without an amount just before this one
    for position, line in enumerate(lines):
        if line.cents is None:
            run += 1
            continue
        if run and line.quantity == run + 1:
            priced += _split(line.cents, run + 1)
        else:
            problems += _no_amount(position - run, run)
            priced.append(PricedLine(line.quantity, line.unit_price_cents, line.cents))
        run = 0
    problems += _no_amount(len(lines) - run, run)
    if problems:
        raise PrintedValueError("; ".join(problems))
    return priced


def _split(cents: int, parts: int) -> list[PricedLine]:
    share = cents // parts
    shares = [share] * (parts - 1) + [cents - share * (parts - 1)]
    return [PricedLine(Decimal(1), None, part) for part in shares]


def _no_amount(first: int, count: int) -> list[str]:
    return [f"line {first + offset + 1}: no amount printed" for offset in range(count)]
