"""The Money value object (S1-AC-01, ADR-0006)."""

import dataclasses
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paper_trail.domain.errors import (
    CurrencyMismatchError,
    DomainValidationError,
    InvalidMoneyError,
)
from paper_trail.domain.money import Money, sum_money

cents = st.integers(min_value=-(10**12), max_value=10**12)
currencies = st.sampled_from(["CAD", "USD", "EUR"])


def test_s1_ac_01_defaults_to_canadian_dollars() -> None:
    """S1-AC-01: the currency defaults to CAD."""
    assert Money(100).currency == "CAD"


@pytest.mark.parametrize("currency", ["cad", "CA", "CADD", "", "C4D", "ÇAD", " CAD"])
def test_s1_ac_01_rejects_non_iso_currency_codes(currency: str) -> None:
    """S1-AC-01: the currency must match ^[A-Z]{3}$."""
    with pytest.raises(InvalidMoneyError):
        Money(100, currency)


@pytest.mark.parametrize("amount", [1.5, Decimal("1"), "100", True])
def test_s1_ac_01_rejects_amounts_that_are_not_integer_cents(amount: object) -> None:
    """S1-AC-01 (pinned in docs/tests/stage1.md): amount_cents is a real int, never float/bool."""
    with pytest.raises(InvalidMoneyError):
        Money(amount, "CAD")  # type: ignore[arg-type]


def test_s1_ac_01_money_errors_are_validation_errors() -> None:
    """Pinned: the Money errors map to 422 problems through DomainValidationError."""
    assert issubclass(InvalidMoneyError, DomainValidationError)
    assert issubclass(CurrencyMismatchError, DomainValidationError)


def test_s1_ac_01_adds_and_subtracts_in_the_same_currency() -> None:
    """S1-AC-01: + and - work on cents and keep the currency."""
    assert Money(250, "USD") + Money(199, "USD") == Money(449, "USD")
    assert Money(250, "USD") - Money(300, "USD") == Money(-50, "USD")
    assert -Money(250) == Money(-250)


@pytest.mark.parametrize("operation", ["add", "sub"])
def test_s1_ac_01_mixing_currencies_raises(operation: str) -> None:
    """S1-AC-01: + and - with different currencies raise CurrencyMismatchError."""
    cad, usd = Money(100, "CAD"), Money(100, "USD")

    combine = {"add": Money.__add__, "sub": Money.__sub__}[operation]

    with pytest.raises(CurrencyMismatchError):
        combine(cad, usd)


def test_s1_ac_01_times_rounds_half_up_to_the_cent() -> None:
    """S1-AC-01: 10.99/kg x 0.452 kg = 4.96748 -> 497 cents (ROUND_HALF_UP)."""
    assert Money(1099).times(Decimal("0.452")) == Money(497)


@pytest.mark.parametrize(
    ("amount", "quantity", "expected"),
    [
        (1, Decimal("0.5"), 1),  # 0.5 rounds up
        (3, Decimal("0.5"), 2),  # 1.5 rounds up, not to even
        (-1, Decimal("0.5"), -1),  # half away from zero
        (499, Decimal("2"), 998),
        (1000, Decimal("0.333"), 333),
    ],
)
def test_s1_ac_01_times_rounding_cases(amount: int, quantity: Decimal, expected: int) -> None:
    """S1-AC-01: ROUND_HALF_UP is half away from zero, never banker's rounding."""
    assert Money(amount, "EUR").times(quantity) == Money(expected, "EUR")


@pytest.mark.parametrize(
    ("amount", "quantity", "expected"),
    [(999, Decimal("2"), 500), (1000, Decimal("3"), 333), (-999, Decimal("2"), -500)],
)
def test_s1_ac_01_divided_by_rounds_half_up_to_the_cent(
    amount: int, quantity: Decimal, expected: int
) -> None:
    """divided_by (a unit price from a line total) rounds like times, half away from zero."""
    assert Money(amount).divided_by(quantity) == Money(expected)


def test_s1_ac_01_amount_text_is_a_plain_two_decimal_number() -> None:
    """amount_text is what arithmetic messages print: no currency, two decimals, a sign."""
    assert [Money(cents).amount_text for cents in (998, -100, 5, 0)] == [
        "9.98",
        "-1.00",
        "0.05",
        "0.00",
    ]


def test_s1_ac_01_is_immutable() -> None:
    """S1-AC-01: Money is a frozen value object."""
    money = Money(100)

    with pytest.raises(dataclasses.FrozenInstanceError):
        money.amount_cents = 200  # type: ignore[misc]


def test_s1_ac_01_is_hashable_and_compared_by_value() -> None:
    """S1-AC-01: equal amounts in the same currency are equal and hash alike."""
    assert Money(100, "CAD") == Money(100, "CAD")
    assert Money(100, "CAD") != Money(100, "USD")
    assert len({Money(100), Money(100), Money(101)}) == 2


def test_s1_ac_01_zero_and_sign() -> None:
    """Money.zero() and is_negative()."""
    assert Money.zero() == Money(0, "CAD")
    assert Money.zero("USD") == Money(0, "USD")
    assert Money(-1).is_negative()
    assert not Money(0).is_negative()
    assert not Money(1).is_negative()


def test_s1_ac_01_sum_money() -> None:
    """sum_money adds values of one currency; an empty sum is zero in that currency."""
    assert sum_money([Money(100, "USD"), Money(-30, "USD")], "USD") == Money(70, "USD")
    assert sum_money([], "EUR") == Money(0, "EUR")
    with pytest.raises(CurrencyMismatchError):
        sum_money([Money(100, "USD")], "CAD")


@given(a=cents, b=cents, c=cents, currency=currencies)
def test_s1_ac_01_property_addition_is_exact_integer_arithmetic(
    a: int, b: int, c: int, currency: str
) -> None:
    """Property: + and - are exact (associative, inverse) like the integers they wrap."""
    x, y, z = Money(a, currency), Money(b, currency), Money(c, currency)

    assert (x + y) + z == x + (y + z)
    assert (x + y) - y == x
    assert x + (-x) == Money.zero(currency)


@given(amount=cents, quantity=st.decimals(min_value=Decimal("0.001"), max_value=1000, places=3))
def test_s1_ac_01_property_times_is_within_half_a_cent(amount: int, quantity: Decimal) -> None:
    """Property: times() differs from the exact product by at most half a cent."""
    exact = Decimal(amount) * quantity

    result = Money(amount).times(quantity).amount_cents

    assert abs(Decimal(result) - exact) <= Decimal("0.5")
