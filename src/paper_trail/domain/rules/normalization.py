"""Normalizing the text that identifies products and stores, so that spelling variants meet.

Both normalizers are idempotent: they repeat their removals until nothing changes.
"""

import re
import unicodedata

# Trailing tokens that receipts print after an item but that do not name it.
_TAX_FLAGS = frozenset({"h", "gst", "hst", "pst", "qst"})
_PRICE = re.compile(r"\$?[0-9]+\.[0-9]{2}-?")
_ITEM_CODE = re.compile(r"[0-9]{4,}")
# A leading item code and the text after it: Costco 4-7 digits, a PLU 4, a UPC 11-13.
_LEADING_ITEM_CODE = re.compile(r"([0-9]{4,13})\s+(\S.*)", re.DOTALL)

# Store-name tokens that are legal forms or store formats, not the store.
_STORE_SUFFIXES = frozenset(
    {"inc", "ltd", "llc", "corp", "co", "wholesale", "supercentre", "supercenter"}
)
_STORE_NUMBER = re.compile(r"#[0-9]+")
_NUMBER = re.compile(r"[0-9]+")
_STORE_WORD = "store"


# Normalized aliases that name a kind of line, not a product (PLAN 6 risks): linking one would
# send every future "PRODUCE" line to the same product. Each entry is its own normalized form.
GENERIC_ALIASES = frozenset(
    {
        "produce",
        "deposit",
        "bottle deposit",
        "container deposit",
        "grocery",
        "groceries",
        "misc",
        "miscellaneous",
        "item",
        "items",
        "department",
        "meat",
        "deli",
        "bakery",
        "dairy",
        "frozen",
        "fee",
        "bag fee",
        "eco fee",
        "discount",
        "coupon",
        "savings",
        "instant savings",
    }
)


def is_generic_alias(alias: str) -> bool:
    """Whether a normalized alias names no product: empty, or on the stop-list."""
    return not alias or alias in GENERIC_ALIASES


def _tokens(text: str) -> list[str]:
    """NFKC, case-folded, whitespace-separated tokens (tabs and no-break spaces included)."""
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())
    return folded.split()


def normalize_alias(text: str) -> str:
    """The alias of a receipt line: e.g. ``"1234567 KS ORG EGGS 24CT 8.99 H"`` -> ``"ks org eggs
    24ct"``. Size tokens, units, ``%`` and accents are kept."""
    tokens = _tokens(text)
    while True:
        if len(tokens) > 1 and _ITEM_CODE.fullmatch(tokens[0]):
            tokens = tokens[1:]
        elif tokens and (tokens[-1] in _TAX_FLAGS or _PRICE.fullmatch(tokens[-1])):
            tokens = tokens[:-1]
        else:
            return " ".join(tokens)


def split_item_code(raw_text: str) -> tuple[str | None, str]:
    """A line's leading item code and the text after it: ``"1281 1% MILK"`` -> ``("1281",
    "1% MILK")``, so that a code is never read as a size. Without one: ``(None, raw_text)``."""
    if match := _LEADING_ITEM_CODE.fullmatch(raw_text):
        return match[1], match[2]
    return None, raw_text


def normalize_store_name(text: str) -> str:
    """The key of a store: e.g. ``"COSTCO WHOLESALE #1234"`` -> ``"costco"``.

    Drops store numbers (``#123``, ``store 123``, a bare trailing number) and legal-form or
    store-format suffixes (``inc``, ``corp.``, ``wholesale``, ``supercentre`` ...).
    """
    tokens = _tokens(text)
    while True:
        shorter = _without_store_numbers(tokens)
        if shorter and (
            _NUMBER.fullmatch(shorter[-1]) or shorter[-1].rstrip(".,") in _STORE_SUFFIXES
        ):
            shorter = shorter[:-1]
        if shorter == tokens:
            return " ".join(tokens)
        tokens = shorter


def _without_store_numbers(tokens: list[str]) -> list[str]:
    kept: list[str] = []
    for token in tokens:
        if _STORE_NUMBER.fullmatch(token):
            continue
        if kept and kept[-1] == _STORE_WORD and _NUMBER.fullmatch(token):
            kept.pop()
            continue
        kept.append(token)
    return kept
