"""Luhn checksum (S1-AC-03)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paper_trail.domain.rules.luhn import luhn_valid
from tests.pans import with_check_digit

# Published test card numbers (Visa, Mastercard, Amex, Discover, Diners); not real cards.
VALID_TEST_PANS = [
    "4111111111111111",
    "5555555555554444",
    "378282246310005",
    "6011111111111117",
    "30569309025904",
    "4012888888881881",
]


@pytest.mark.parametrize("pan", VALID_TEST_PANS)
def test_s1_ac_03_known_test_pans_are_valid(pan: str) -> None:
    """S1-AC-03: published test PANs pass the Luhn check."""
    assert luhn_valid(pan)


@pytest.mark.parametrize("pan", VALID_TEST_PANS)
def test_s1_ac_03_changing_any_single_digit_breaks_validity(pan: str) -> None:
    """S1-AC-03: every single-digit substitution of a valid number is invalid."""
    for index, original in enumerate(pan):
        for digit in "0123456789":
            if digit != original:
                mutated = pan[:index] + digit + pan[index + 1 :]
                assert not luhn_valid(mutated), mutated


@pytest.mark.parametrize(
    "value",
    ["", "0", "4111 1111 1111 1111", "4111-1111-1111-1111", "41111111111111x1", "٤١١١١١١١١١١١١١١١"],
)
def test_s1_ac_03_non_digit_or_too_short_input_is_invalid(value: str) -> None:
    """S1-AC-03: digits only (ASCII), at least two of them; anything else is False."""
    assert not luhn_valid(value)


def test_s1_ac_03_two_digits_is_the_minimum() -> None:
    """S1-AC-03: "00" and "18" are valid two-digit Luhn numbers; "10" is not."""
    assert luhn_valid("00")
    assert luhn_valid("18")
    assert not luhn_valid("10")


@given(
    body=st.text(alphabet="0123456789", min_size=1, max_size=30),
    data=st.data(),
)
def test_s1_ac_03_property_single_digit_change_is_always_detected(
    body: str, data: st.DataObject
) -> None:
    """Property: a valid number with any one digit replaced is invalid."""
    number = with_check_digit(body)
    index = data.draw(st.integers(min_value=0, max_value=len(number) - 1))
    replacement = data.draw(st.sampled_from([d for d in "0123456789" if d != number[index]]))

    assert luhn_valid(number)
    assert not luhn_valid(number[:index] + replacement + number[index + 1 :])
