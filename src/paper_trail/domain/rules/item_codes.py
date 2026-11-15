"""The code printed at the start of a receipt line, read as a product identity (barcode-lookup
plan section 2, ADR-0018).

A GTIN or PLU names one product at every store; a store's own item number or in-store code
names one only at that store. Costco prints its own item numbers, so every Costco code is a
store item; every other store uses the default profile. Nothing here opens a connection:
``lookup_url`` only builds a link a person may open.
"""

import re
from dataclasses import dataclass
from urllib.parse import quote

from paper_trail.domain.enums import CodeKind, CodeScheme
from paper_trail.domain.models import CodeIdentity
from paper_trail.domain.rules.normalization import is_trailing_noise, split_item_code

# Stores whose every code is their own item number, by ``normalize_store_name`` key.
STORE_ITEM_STORES = frozenset({"costco"})
STORE_SEARCH_URLS = {"costco": "https://www.costco.ca/CatalogSearch?keyword={code}"}

_DIGITS = re.compile(r"[0-9]+")
_BARE_CODE = re.compile(r"(?:\([0-9]{1,2}\)\s*)?[0-9]+")
_GTIN_LENGTHS = frozenset({8, 12, 13, 14})
_PLU = 4
_ORGANIC_PLU = 5
_EAN_8 = 8
_UPC_A = 12
_EAN_13 = 13
PLU_RANGE = range(3000, 5000)  # IFPS produce codes; a leading 9 marks the organic one
_ORGANIC_PREFIX = "9"
# GS1 restricted circulation: prefixes 02, 04 and 20-29 are a store's own (in-store) codes.
_RESTRICTED_PREFIXES = ("02", "04", *(f"2{digit}" for digit in range(10)))
# Leading digit of an 11-digit Loblaw UPC body: 2 variable measure, 4 in-store, 5 a coupon.
_IN_STORE_UPC = frozenset({"2", "4"})
_COUPON_UPC = "5"


@dataclass(frozen=True, slots=True)
class ItemCode:
    """A line's leading code. ``keys`` are what a reference source (``plu.csv``, Open Food
    Facts) is asked for, best first; ``identities`` what ``product_codes`` may hold it under,
    best first. They differ only for an organic PLU: identity ``94046``, key ``4046``."""

    kind: CodeKind
    printed: str  # the digits as printed, a ``(N)`` marker removed
    keys: tuple[str, ...]
    scheme: CodeScheme | None  # None for NONE
    organic: bool = False  # a PLU with the 9 prefix

    @property
    def identities(self) -> tuple[str, ...]:
        """GTIN: ``keys``; PLU and store codes: ``(printed,)``; NONE: ``()``."""
        if self.scheme is None:
            return ()
        return self.keys if self.scheme is CodeScheme.GTIN else (self.printed,)

    @property
    def identity(self) -> str | None:
        """The first identity: what a new ``product_codes`` row stores. None for NONE."""
        identities = self.identities
        return identities[0] if identities else None

    def identity_at(self, store_key: str) -> CodeIdentity | None:
        """``identity`` with its scheme and store (``""`` unless STORE). None for NONE, and for
        a store's own code read without a store (it names nothing anywhere else)."""
        identity = self.identity
        if self.scheme is None or identity is None:
            return None
        if self.scheme is CodeScheme.STORE and not store_key:
            return None
        return CodeIdentity(
            self.scheme, store_key if self.scheme is CodeScheme.STORE else "", identity
        )


def gs1_check_digit(body: str) -> str:
    """The GS1 mod-10 check digit of a digit string (weights 3, 1, 3, ... from the right)."""
    total = sum(int(digit) * (3 if i % 2 == 0 else 1) for i, digit in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)


def is_valid_gtin(code: str) -> bool:
    """An 8, 12, 13 or 14 digit code whose last digit is its check digit."""
    return (
        bool(_DIGITS.fullmatch(code))
        and len(code) in _GTIN_LENGTHS
        and gs1_check_digit(code[:-1]) == code[-1]
    )


def off_key(gtin: str) -> str:
    """Open Food Facts' form of a barcode: leading zeros stripped, then 1-7 digits padded to 8
    and 9-12 digits padded to 13."""
    stripped = gtin.lstrip("0")
    if len(stripped) <= _EAN_8:
        return stripped.zfill(_EAN_8)
    return stripped.zfill(_EAN_13)


def parse_item_code(raw_text: str, store_key: str) -> ItemCode | None:
    """The line's leading code read with the store's profile (``STORE_ITEM_STORES`` or the
    default one); None when the line has no leading code (``split_item_code``)."""
    printed, _ = split_item_code(raw_text)
    if printed is None:
        return None
    if store_key in STORE_ITEM_STORES:
        return _store(CodeKind.STORE_ITEM, printed)
    return _default_profile(printed)


def parse_bare_code(code: str, store_key: str) -> ItemCode | None:
    """A code typed alone (``catalog add``/``remove``/``lookup``) read exactly as the line
    ``"<code> X"`` would be (``parse_item_code``); None when ``code`` is not only digits (a
    ``(N)`` marker in front allowed) as the splitter accepts them."""
    if not _BARE_CODE.fullmatch(code):
        return None
    return parse_item_code(f"{code} X", store_key)


def printed_name(raw_text: str) -> str:
    """The name of a product no source knows: the line without its leading code and trailing
    tax-flag or price tokens, case and abbreviations kept; ``raw_text`` when nothing is left."""
    _, text = split_item_code(raw_text)
    tokens = text.split()
    while tokens and is_trailing_noise(tokens[-1]):
        tokens.pop()
    return " ".join(tokens) or raw_text


def lookup_url(store_key: str, code: str) -> str | None:
    """The store's search link for a code as printed (URL-quoted, zeros kept), None for a store
    without one. Built for a person to open; never requested."""
    template = STORE_SEARCH_URLS.get(store_key)
    return None if template is None else template.format(code=quote(code, safe=""))


def _store(kind: CodeKind, printed: str) -> ItemCode:
    return ItemCode(kind, printed, (printed,), CodeScheme.STORE)


def _gtin(printed: str, *keys: str) -> ItemCode:
    return ItemCode(CodeKind.GTIN, printed, keys, CodeScheme.GTIN)


def _none(printed: str) -> ItemCode:
    return ItemCode(CodeKind.NONE, printed, (), None)


def _default_profile(printed: str) -> ItemCode:  # noqa: PLR0911 - one return per row of the table
    """Design section 2's classification table, by the number of digits."""
    match len(printed):
        case 4 | 5 | 6:
            return _short(printed)
        case 7:
            return _store(CodeKind.IN_STORE if printed[0] == "2" else CodeKind.STORE_ITEM, printed)
        case 8:
            if is_valid_gtin(printed):
                return _gtin(printed, off_key(printed))
            return _store(CodeKind.STORE_ITEM, printed)
        case 11:
            return _upc_body(printed)
        case 12 | 13 | 14:
            return _long(printed)
        case _:
            return _none(printed)


def _long(printed: str) -> ItemCode:
    """12-14 digits: a UPC-A (its number system read as for 11 digits; both readings when its
    check is valid), an EAN-13, or a GTIN-14 with a leading 0. A code that reads as a
    restricted-circulation EAN-13 is a store's own."""
    if len(printed) == _UPC_A:
        return _upc_a(printed)
    if not is_valid_gtin(printed) or (len(printed) > _EAN_13 and printed[0] != "0"):
        return _none(printed)
    if printed[-_EAN_13:].startswith(_RESTRICTED_PREFIXES):
        return _store(CodeKind.IN_STORE, printed)
    return _gtin(printed, off_key(printed))


def _upc_a(printed: str) -> ItemCode:
    """12 digits: a valid UPC-A by its number system, both readings offered; else the body of
    an EAN-13 with its check digit computed."""
    ean_body = printed + gs1_check_digit(printed)
    if not is_valid_gtin(printed):
        if ean_body.startswith(_RESTRICTED_PREFIXES):
            return _store(CodeKind.IN_STORE, printed)
        return _gtin(printed, off_key(ean_body))
    if printed[0] in _IN_STORE_UPC:
        return _store(CodeKind.IN_STORE, printed)
    if printed[0] == _COUPON_UPC:
        return _none(printed)
    return _gtin(printed, off_key("0" + printed), off_key(ean_body))


def _short(printed: str) -> ItemCode:
    """4-6 digits: an IFPS PLU (organic with the 9 prefix), else a store's own code."""
    organic = len(printed) == _ORGANIC_PLU and printed[0] == _ORGANIC_PREFIX
    plain = printed[1:] if organic else printed
    if len(plain) == _PLU and int(plain) in PLU_RANGE:
        return ItemCode(CodeKind.PLU, printed, (plain,), CodeScheme.PLU, organic=plain != printed)
    return _store(CodeKind.STORE_ITEM, printed)


def _upc_body(printed: str) -> ItemCode:
    """11 digits: a UPC-A without its check digit (Loblaw)."""
    if printed[0] in _IN_STORE_UPC:
        return _store(CodeKind.IN_STORE, printed)
    if printed[0] == _COUPON_UPC:
        return _none(printed)
    return _gtin(printed, off_key("0" + printed + gs1_check_digit(printed)))
