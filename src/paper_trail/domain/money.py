"""Money as integer minor units plus an ISO 4217 currency code (ADR-0006)."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Self

from paper_trail.domain.errors import CurrencyMismatchError, InvalidMoneyError

DEFAULT_CURRENCY = "CAD"
CENTS_PER_UNIT = 100

_CURRENCY_CODE = re.compile(r"[A-Z]{3}")
_WHOLE_CENT = Decimal(1)


@dataclass(frozen=True, slots=True)
class Money:
    """An amount in cents of one currency. Immutable, hashable and compared by value.

    Every currency Paper Trail handles has two minor-unit digits, so "cents" is literal.
    """

    amount_cents: int
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        # bool is an int subclass, and a float or Decimal amount would smuggle in fractions.
        if type(self.amount_cents) is not int:
            msg = f"amount_cents must be an int, got {type(self.amount_cents).__name__}"
            raise InvalidMoneyError(msg)
        if not isinstance(self.currency, str) or not _CURRENCY_CODE.fullmatch(self.currency):
            msg = f"currency must be a three-letter ISO 4217 code, got {self.currency!r}"
            raise InvalidMoneyError(msg)

    @classmethod
    def zero(cls, currency: str = DEFAULT_CURRENCY) -> Self:
        return cls(0, currency)

    def __add__(self, other: "Money") -> "Money":
        return Money(self.amount_cents + self._same_currency(other).amount_cents, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        return Money(self.amount_cents - self._same_currency(other).amount_cents, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount_cents, self.currency)

    def times(self, quantity: Decimal) -> "Money":
        """This amount times ``quantity``, rounded half away from zero to the cent."""
        return self._rounded(Decimal(self.amount_cents) * quantity)

    def divided_by(self, quantity: Decimal) -> "Money":
        """This amount divided by ``quantity`` (e.g. a unit price), rounded like ``times``."""
        return self._rounded(Decimal(self.amount_cents) / quantity)

    def is_negative(self) -> bool:
        return self.amount_cents < 0

    @property
    def amount_text(self) -> str:
        """The amount as a plain decimal number without the currency, e.g. ``"-1.00"``."""
        sign = "-" if self.amount_cents < 0 else ""
        units, cents = divmod(abs(self.amount_cents), CENTS_PER_UNIT)
        return f"{sign}{units}.{cents:02d}"

    def _rounded(self, cents: Decimal) -> "Money":
        return Money(int(cents.quantize(_WHOLE_CENT, rounding=ROUND_HALF_UP)), self.currency)

    def _same_currency(self, other: "Money") -> "Money":
        if other.currency != self.currency:
            msg = f"cannot combine {self.currency} with {other.currency}"
            raise CurrencyMismatchError(msg)
        return other


def sum_money(values: Iterable[Money], currency: str) -> Money:
    """The sum of ``values``, all in ``currency`` (zero when empty)."""
    total = Money.zero(currency)
    for value in values:
        total += value
    return total
