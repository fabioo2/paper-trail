"""Alias and store-name normalization (S1-AC-06).

Pinned (docs/tests/stage1.md): trailing tax flags are the tokens h, gst, hst, pst, qst; trailing
prices are amounts with two decimals (optionally "$"-prefixed or "-"-suffixed); store suffixes
are inc, ltd, llc, corp, co, wholesale, supercentre, supercenter; store numbers are "#123",
"store 123" or a bare trailing number.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paper_trail.domain.rules.normalization import normalize_alias, normalize_store_name

# Letters, digits, punctuation, whitespace and a few characters NFKC changes (ligature,
# full-width letter, no-break space, accents).
_text = st.text(
    alphabet="ABCHKSTabchkst0123456789 .#$-%\t\u00a0\ufb01\uff2b\u00e9\u00c9", max_size=40
)


@pytest.mark.parametrize(
    "raw",
    [
        "KS ORG EGGS 24CT",
        "ks  org eggs 24ct ",
        "KS ORG EGGS 24CT H",
        "  KS\tORG EGGS 24CT  GST",
        "1234567 KS ORG EGGS 24CT",
        "KS ORG EGGS 24CT 8.99",
        "KS ORG EGGS 24CT $8.99 H",
        "KS ORG EGGS 24CT 3.00-",
        "\uff2b\uff33 ORG EGGS 24CT",  # full-width "KS"
    ],
)
def test_s1_ac_06_alias_variants_normalize_to_the_same_string(raw: str) -> None:
    """S1-AC-06: case, spacing, item codes, prices and tax flags do not change the alias."""
    assert normalize_alias(raw) == "ks org eggs 24ct"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("KS 2% MILK 4L", "ks 2% milk 4l"),
        ("BANANAS 1.36KG", "bananas 1.36kg"),
        ("GREEK YOGURT 750G H", "greek yogurt 750g"),
        ("COFFEE 2X1LB", "coffee 2x1lb"),
        ("SPARKLING WATER 24 X 355ML", "sparkling water 24 x 355ml"),
        ("VITAMIN E", "vitamin e"),
        ("CRÈME FRAÎCHE", "crème fraîche"),
        ("ﬁsh sticks", "fish sticks"),
    ],
)
def test_s1_ac_06_alias_keeps_size_tokens_and_words(raw: str, expected: str) -> None:
    """S1-AC-06: size tokens, units and digits inside the name are kept."""
    assert normalize_alias(raw) == expected


@given(text=_text)
def test_s1_ac_06_property_normalize_alias_is_idempotent(text: str) -> None:
    """S1-AC-06: normalizing a normalized alias changes nothing."""
    once = normalize_alias(text)

    assert normalize_alias(once) == once


@given(text=_text)
def test_s1_ac_06_property_alias_has_no_outer_or_double_whitespace(text: str) -> None:
    """Whitespace is collapsed to single spaces and stripped; the result is case-folded."""
    alias = normalize_alias(text)

    assert alias == alias.strip()
    assert "  " not in alias
    assert "\t" not in alias
    assert alias == alias.casefold()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("COSTCO WHOLESALE #1234", "costco"),
        ("Costco", "costco"),
        ("Costco Wholesale Corp.", "costco"),
        ("Walmart Supercentre 3150", "walmart"),
        ("WALMART", "walmart"),
        ("Walmart Supercenter #5939", "walmart"),
        ("Loblaws Inc.", "loblaws"),
        ("METRO Store 123", "metro"),
        ("  Shoppers   Drug Mart #1081 ", "shoppers drug mart"),
        ("Farm Boy Ltd", "farm boy"),
    ],
)
def test_s1_ac_06_store_names_normalize_to_their_key(raw: str, expected: str) -> None:
    """S1-AC-06: store numbers and legal or format suffixes are dropped."""
    assert normalize_store_name(raw) == expected


@given(text=_text)
def test_s1_ac_06_property_normalize_store_name_is_idempotent(text: str) -> None:
    """A store key normalizes to itself."""
    once = normalize_store_name(text)

    assert normalize_store_name(once) == once
