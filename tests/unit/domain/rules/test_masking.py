"""Card-number masking before persistence (S1-AC-04, ADR-0011)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paper_trail.domain.rules.masking import MaskResult, mask_pans
from tests.pans import card_numbers, with_check_digit


def test_s1_ac_04_masks_a_space_separated_pan() -> None:
    """S1-AC-04: the example from the plan."""
    result = mask_pans("VISA 4111 1111 1111 1111")

    assert result == MaskResult(text="VISA **** **** **** 1111", masked_count=1)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4111-1111-1111-1111", "****-****-****-1111"),
        ("4111111111111111", "************1111"),
        ("AMEX 3782 822463 10005", "AMEX **** ****** *0005"),
        ("card:5555555555554444.", "card:************4444."),
        ("6011111111111117 approved", "************1117 approved"),
    ],
)
def test_s1_ac_04_masks_every_separator_style_keeping_the_last_four(
    text: str, expected: str
) -> None:
    """S1-AC-04: dash-separated and unseparated forms are masked; separators are preserved."""
    result = mask_pans(text)

    assert result.text == expected
    assert result.masked_count == 1


def test_s1_ac_04_counts_every_masked_number() -> None:
    """S1-AC-04: masked_count counts each masked candidate."""
    result = mask_pans("4111111111111111\nREFUND TO 5555 5555 5555 4444")

    assert result.text == "************1111\nREFUND TO **** **** **** 4444"
    assert result.masked_count == 2


@pytest.mark.parametrize(
    "text",
    [
        "036000291452",  # 12-digit UPC
        "UPC 0 36000 29145 2",
        "4111111111111112",  # 16 digits, fails Luhn
        "4111 1111 1111 1112",
        "2026-11-06",
        "06/11/2026 14:32",
        "TOTAL 1234.56",
        "$4,111.11",
        "KS ORG EGGS 24CT 8.99 H",
        "(613) 555-0199",
        "1-800-555-0199",
        "41111111111111111111",  # 20 digits: longer than any PAN
        "4111  1111 1111 1111",  # two spaces split the run into short pieces
        "**** **** **** 1111",  # already masked
        "",
    ],
)
def test_s1_ac_04_leaves_everything_else_unchanged(text: str) -> None:
    """S1-AC-04: UPCs, non-Luhn numbers, dates, prices, phones and masked text are untouched."""
    assert mask_pans(text) == MaskResult(text=text, masked_count=0)


def test_s1_ac_04_thirteen_and_nineteen_digit_numbers_are_masked() -> None:
    """S1-AC-04: the candidate length range is 13..19 inclusive."""
    thirteen = with_check_digit("422222222222")
    nineteen = with_check_digit("622222222222222222")

    assert mask_pans(thirteen).text == "*" * 9 + thirteen[-4:]
    assert mask_pans(nineteen).text == "*" * 15 + nineteen[-4:]


# Text made of the pieces receipts are made of, with real (Luhn-valid) card numbers mixed in.
_SEPARATOR = st.sampled_from(["", " ", "-"])


@st.composite
def _card_number(draw: st.DrawFn) -> str:
    body = draw(st.text(alphabet="0123456789", min_size=12, max_size=18))
    digits = with_check_digit(body)
    separators = [draw(_SEPARATOR) for _ in digits[1:]]
    return digits[0] + "".join(sep + d for sep, d in zip(separators, digits[1:], strict=True))


_receipt_text = st.lists(
    st.one_of(
        _card_number(),
        st.text(alphabet="0123456789 -*/.$:", max_size=25),
        st.text(alphabet="ABCXYZ abc\n", max_size=10),
    ),
    max_size=8,
).map("".join)


@given(text=_receipt_text)
def test_s1_ac_04_property_masking_is_idempotent(text: str) -> None:
    """S1-AC-04: masking masked text changes nothing and masks nothing."""
    once = mask_pans(text)

    assert mask_pans(once.text) == MaskResult(text=once.text, masked_count=0)


@given(text=_receipt_text)
def test_s1_ac_04_property_output_has_no_card_number(text: str) -> None:
    """S1-AC-04: no Luhn-valid 13-19 digit run survives masking."""
    assert card_numbers(mask_pans(text).text) == []


@given(text=_receipt_text)
def test_s1_ac_04_property_only_digits_change_and_only_to_stars(text: str) -> None:
    """Masking keeps the length and every non-digit; a changed character is a digit now '*'."""
    masked = mask_pans(text).text

    assert len(masked) == len(text)
    for before, after in zip(text, masked, strict=True):
        assert after == before or (before.isdigit() and after == "*")


@given(text=_receipt_text)
def test_s1_ac_04_property_masked_count_matches_the_card_numbers_found(text: str) -> None:
    """masked_count equals the number of card numbers in the input."""
    assert mask_pans(text).masked_count == len(card_numbers(text))
