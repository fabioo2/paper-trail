"""The code printed at the start of a receipt line (barcode-lookup plan R1, R2, A5).

Pinned (test-cases.md PC-TC-01..08, 10; TQ37): GS1 check digits, the OFF key, the classification
of a leading code under the default profile and the Costco profile ("every code is a store item"),
the organic PLU's identity vs its lookup key, the printed name, the store search link, and a
bare code (``catalog add``/``remove``/``lookup``) read exactly as ``"<code> X"``. The
codes are synthetic, quoted by the plan, or textbook barcodes.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from paper_trail.domain.enums import CodeKind, CodeScheme
from paper_trail.domain.models import CodeIdentity
from paper_trail.domain.rules.item_codes import (
    STORE_SEARCH_URLS,
    ItemCode,
    gs1_check_digit,
    is_valid_gtin,
    lookup_url,
    off_key,
    parse_bare_code,
    parse_item_code,
    printed_name,
)
from paper_trail.domain.rules.normalization import split_item_code

GTIN = CodeKind.GTIN
PLU = CodeKind.PLU
IN_STORE = CodeKind.IN_STORE
STORE_ITEM = CodeKind.STORE_ITEM
NONE = CodeKind.NONE


def _digits(lengths: tuple[int, ...]) -> st.SearchStrategy[str]:
    return st.sampled_from(lengths).flatmap(
        lambda n: st.text(alphabet="0123456789", min_size=n, max_size=n)
    )


@pytest.mark.parametrize(
    ("body", "digit"),
    [
        ("03400047069", "3"),
        ("06038306487", "7"),
        ("05889165231", "0"),
        ("64843610117", "7"),
        ("05700001285", "4"),
        ("06340012135", "5"),
        ("7351353", "7"),  # EAN-8 body
        ("400638133393", "1"),  # EAN-13 body
        ("0001234560001", "2"),  # GTIN-14 body
    ],
)
def test_pc_tc_01_gs1_check_digit(body: str, digit: str) -> None:
    assert gs1_check_digit(body) == digit


@pytest.mark.parametrize(
    ("code", "valid"),
    [
        ("73513537", True),
        ("036000291452", True),
        ("4006381333931", True),
        ("00012345600012", True),
        ("036000291453", False),
        ("4006381333930", False),
        ("03400047069", False),  # 11 digits
        ("0360002914", False),  # 10
        ("7351353", False),  # 7
        ("123456789012345", False),  # 15
        ("03600029145X", False),
        ("", False),
    ],
)
def test_pc_tc_02_is_valid_gtin(code: str, valid: bool) -> None:
    assert is_valid_gtin(code) is valid


@pytest.mark.parametrize(
    ("gtin", "key"),
    [
        ("036000291452", "0036000291452"),
        ("0060383064877", "0060383064877"),
        ("4006381333931", "4006381333931"),
        ("73513537", "73513537"),
        ("0000073513537", "73513537"),
        ("00012345600012", "0012345600012"),
    ],
)
def test_pc_tc_03_off_key(gtin: str, key: str) -> None:
    assert off_key(gtin) == key


@given(code=_digits((8, 9, 10, 11, 12, 13, 14)))
def test_pc_tc_03_property_off_key_is_idempotent(code: str) -> None:
    assert off_key(off_key(code)) == off_key(code)


def _plu(code: str) -> tuple[str, ItemCode, str]:
    return code, ItemCode(PLU, code, (code,), CodeScheme.PLU), code


def _organic(code: str) -> tuple[str, ItemCode, str]:
    return code, ItemCode(PLU, code, (code[1:],), CodeScheme.PLU, organic=True), code


def _store(code: str, kind: CodeKind = STORE_ITEM) -> tuple[str, ItemCode, str]:
    return code, ItemCode(kind, code, (code,), CodeScheme.STORE), code


def _gtin(code: str, *keys: str) -> tuple[str, ItemCode, str]:
    return code, ItemCode(GTIN, code, keys, CodeScheme.GTIN), keys[0]


def _none(code: str) -> tuple[str, ItemCode, None]:
    return code, ItemCode(NONE, code, (), None), None


DEFAULT_PROFILE = [
    *(_plu(code) for code in ("3000", "3101", "4999")),
    *(_organic(code) for code in ("93000", "93101", "94999")),
    *(_store(code) for code in ("2999", "5000", "92999", "83101", "62021", "123456")),
    _store("2100580", IN_STORE),
    _store("1362394"),
    _gtin("73513537", "73513537"),
    _store("73513538"),
    _none("123456789"),
    _none("0000392208"),
    _gtin("06038306487", "0060383064877"),
    _gtin("05700001285", "0057000012854"),
    _gtin("03400047069", "0034000470693"),
    _gtin("05889165231", "0058891652310"),
    _gtin("64843610117", "0648436101177"),
    _gtin("06340012135", "0063400121355"),
    _store("21234567890", IN_STORE),
    _store("41234567890", IN_STORE),
    _none("51234567890"),
    _gtin("036000291452", "0036000291452", "0360002914522"),
    _gtin("400638133393", "4006381333931"),
    _store("212345005996", IN_STORE),  # UPC-A number system 2: variable measure
    _store("412345000018", IN_STORE),  # number system 4: in-store
    _none("512345000015"),  # number system 5: a coupon
    _gtin("4006381333931", "4006381333931"),
    _gtin("0060383064877", "0060383064877"),
    *(
        _store(code, IN_STORE)
        for code in ("2012345678903", "0212345678909", "0412345678903", "2912345678906")
    ),
    _none("2012345678904"),
    _none("4006381333930"),
    _gtin("00012345600012", "0012345600012"),
    _store("00212345678909", IN_STORE),  # a GTIN-14 around a restricted EAN-13
    _none("12345678901234"),
]


@pytest.mark.parametrize(("printed", "expected", "identity"), DEFAULT_PROFILE)
def test_pc_tc_04_classification_with_the_default_profile(
    printed: str, expected: ItemCode, identity: str | None
) -> None:
    code = parse_item_code(f"{printed} X", "loblaws")

    assert code == expected
    assert code is not None
    assert code.identity == identity


@pytest.mark.parametrize(
    "printed",
    ["3101", "93101", "1362394", "0000392208", "06038306487", "4006381333931", "51234567890"],
)
def test_pc_tc_05_every_costco_code_is_a_store_item(printed: str) -> None:
    code = parse_item_code(f"{printed} X", "costco")

    assert code == ItemCode(STORE_ITEM, printed, (printed,), CodeScheme.STORE)
    assert code is not None
    assert code.identity_at("costco") == CodeIdentity(CodeScheme.STORE, "costco", printed)


@pytest.mark.parametrize("store_key", ["walmart", ""])
def test_pc_tc_05_other_stores_use_the_default_profile(store_key: str) -> None:
    code = parse_item_code("3101 X", store_key)

    assert code == ItemCode(PLU, "3101", ("3101",), CodeScheme.PLU)
    assert code is not None
    assert code.identity_at(store_key) == CodeIdentity(CodeScheme.PLU, "", "3101")


@pytest.mark.parametrize(
    "raw_text",
    [
        "KS ORG EGGS 24CT",
        "12 EGGS",
        "540 ML BEANS",
        "1281",
        "KS 1281 MILK",
        "(2) GOLD KIWI",
        "",
        "123456789012345 X",
    ],
)
def test_pc_tc_06_no_leading_code_is_none(raw_text: str) -> None:
    assert parse_item_code(raw_text, "loblaws") is None


@pytest.mark.parametrize(
    ("raw_text", "store_key", "expected"),
    [
        (
            "(1)05700001285 CRV CHKKN PBLN BR MRJ",
            "loblaws",
            ItemCode(GTIN, "05700001285", ("0057000012854",), CodeScheme.GTIN),
        ),
        (
            "(2) 1362394 CHOC PISTACH",
            "costco",
            ItemCode(STORE_ITEM, "1362394", ("1362394",), CodeScheme.STORE),
        ),
    ],
)
def test_pc_tc_06_the_quantity_marker_is_dropped(
    raw_text: str, store_key: str, expected: ItemCode
) -> None:
    assert parse_item_code(raw_text, store_key) == expected


def test_pc_tc_06_the_splitter_reaches_14_digits() -> None:
    assert split_item_code("00012345600012 X") == ("00012345600012", "X")
    assert split_item_code("123456789012345 X") == (None, "123456789012345 X")


@pytest.mark.parametrize(
    ("raw_text", "identity"),
    [
        ("2100580 X", None),
        ("62021 X", None),
        ("3101 X", CodeIdentity(CodeScheme.PLU, "", "3101")),
        ("06038306487 X", CodeIdentity(CodeScheme.GTIN, "", "0060383064877")),
    ],
    ids=["in-store", "store-item", "plu", "gtin"],
)
def test_a_store_code_has_no_identity_without_a_store(
    raw_text: str, identity: CodeIdentity | None
) -> None:
    """Fable P2: a store's own code names nothing without a store; barcodes and PLUs do."""
    code = parse_item_code(raw_text, "")

    assert code is not None
    assert code.identity_at("") == identity


@given(body=_digits((7, 11, 12, 13)))
def test_pc_tc_07_property_a_computed_check_digit_validates(body: str) -> None:
    assert is_valid_gtin(body + gs1_check_digit(body))


@given(body=_digits((11,)).filter(lambda b: b[0] not in "245"))
def test_pc_tc_07_property_an_11_digit_body_is_one_valid_13_digit_key(body: str) -> None:
    code = parse_item_code(f"{body} X", "loblaws")

    assert code is not None
    assert code.kind is GTIN
    assert code.keys == (off_key(body + gs1_check_digit(body)),)
    assert is_valid_gtin(code.keys[0])
    # off_key pads a code of at most 8 significant digits to 8, not 13 (PC-TC-03's rule).
    assert len(code.keys[0]) == (13 if len(body.lstrip("0")) >= 8 else 8)


@pytest.mark.parametrize(
    ("raw_text", "name"),
    [
        ("06038306487 PC UNS BABYWIPES HMRJ", "PC UNS BABYWIPES"),
        ("3139415 GOLD KIWI", "GOLD KIWI"),
        ("(1)05700001285 CRV CHKKN PBLN BR MRJ", "CRV CHKKN PBLN BR"),
        ("KS ORG EGGS 24CT H", "KS ORG EGGS 24CT"),
        ("1362394 Choc Pistach 16.99", "Choc Pistach"),
        ("TH 2PC PJ", "TH 2PC PJ"),
        ("MRJ COOKIES", "MRJ COOKIES"),
        ("06038306487 HMRJ", "06038306487 HMRJ"),
        ("1281", "1281"),
    ],
)
def test_pc_tc_08_printed_name(raw_text: str, name: str) -> None:
    assert printed_name(raw_text) == name


@pytest.mark.parametrize(
    ("store_key", "code", "url"),
    [
        ("costco", "1362394", "https://www.costco.ca/CatalogSearch?keyword=1362394"),
        ("costco", "0001281", "https://www.costco.ca/CatalogSearch?keyword=0001281"),
        ("costco", "12 34", "https://www.costco.ca/CatalogSearch?keyword=12%2034"),
        ("loblaws", "1362394", None),
        ("", "1", None),
    ],
)
def test_pc_tc_10_lookup_url(store_key: str, code: str, url: str | None) -> None:
    assert lookup_url(store_key, code) == url


def test_pc_tc_10_only_costco_has_a_search_url() -> None:
    assert STORE_SEARCH_URLS == {"costco": "https://www.costco.ca/CatalogSearch?keyword={code}"}


@pytest.mark.parametrize(
    ("code", "store_key"),
    [
        ("1362394", "costco"),
        ("3101", "costco"),
        ("3101", "loblaws"),
        ("93101", "loblaws"),
        ("06038306487", "loblaws"),
        ("036000291452", "loblaws"),
        ("2100580", "loblaws"),
        ("51234567890", "loblaws"),
        ("4006381333930", "loblaws"),
        ("123456789", "loblaws"),
        ("(1)05700001285", "loblaws"),
        ("00012345600012", "walmart"),
    ],
)
def test_tq37_a_bare_code_reads_as_the_code_of_a_line(code: str, store_key: str) -> None:
    assert parse_bare_code(code, store_key) == parse_item_code(f"{code} X", store_key)
    assert parse_bare_code(code, store_key) is not None


@pytest.mark.parametrize("code", ["", "abc", "1362394 X", "12", "123", "123456789012345", " 3101"])
def test_tq37_anything_but_a_bare_code_is_none(code: str) -> None:
    assert parse_bare_code(code, "loblaws") is None
