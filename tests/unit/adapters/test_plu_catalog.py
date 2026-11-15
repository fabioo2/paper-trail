"""``PluCatalog`` and ``CompositeCatalog`` (barcode-lookup PR 1a, R6, A5; PC-TC-30..33, 35).

``PluCatalog(path)`` reads the normalized ``plu.csv`` (``code,name,organic``) on its first lookup
and keeps it in memory. It answers PLU codes only, by their lookup key: an organic ``93101`` is
looked up as ``3101`` and named "Organic " + the table's name with the tag ``organic``. A missing,
unreadable or malformed file is a miss for every lookup with exactly one ``catalog.missing``
warning per catalog (``path``, ``reason``); a header-only file misses silently.
``CompositeCatalog(*catalogs)`` asks its catalogs in order and stops at the first hit.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from paper_trail.adapters.catalog.composite import CompositeCatalog
from paper_trail.adapters.catalog.plu import PluCatalog
from paper_trail.domain.enums import Category
from paper_trail.domain.rules.item_codes import ItemCode, parse_item_code
from paper_trail.ports.catalog import CatalogProduct
from tests.extractions import L1, L3, L4, L5, LOBLAWS_KEY, PLU_CSV
from tests.fakes.catalog import ORGANIC_TEST_APPLE, TEST_APPLE, FakeCatalog


def _code(raw_text: str, store_key: str = LOBLAWS_KEY) -> ItemCode:
    code = parse_item_code(raw_text, store_key)
    assert code is not None
    return code


def _plu_file(tmp_path: Path, text: str = PLU_CSV) -> Path:
    path = tmp_path / "plu.csv"
    path.write_text(text, encoding="utf-8")
    return path


def _missing(logs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == "catalog.missing"]


def test_a_plu_in_the_table_is_a_hit(tmp_path: Path) -> None:
    catalog = PluCatalog(_plu_file(tmp_path))

    assert catalog.lookup(_code(L3), LOBLAWS_KEY) == CatalogProduct(
        "plu", "3101", None, "Test Apple", None, None, None, Category.PRODUCE, ()
    )
    assert catalog.lookup(_code(L3), "walmart") == TEST_APPLE


def test_a_file_saved_with_a_byte_order_mark_hits(tmp_path: Path) -> None:
    """A spreadsheet's "CSV UTF-8" export starts with a BOM; its header is still ``code``."""
    path = tmp_path / "plu.csv"
    path.write_text(PLU_CSV, encoding="utf-8-sig")

    with capture_logs() as logs:
        found = PluCatalog(path).lookup(_code(L3), LOBLAWS_KEY)

    assert found == TEST_APPLE
    assert _missing(logs) == []


def test_an_organic_plu_is_looked_up_by_its_conventional_key_and_named_organic(
    tmp_path: Path,
) -> None:
    catalog = PluCatalog(_plu_file(tmp_path))

    assert catalog.lookup(_code(L4), LOBLAWS_KEY) == ORGANIC_TEST_APPLE


@pytest.mark.parametrize(
    ("raw_text", "store_key"),
    [
        ("4999 X", LOBLAWS_KEY),
        (L1, LOBLAWS_KEY),
        (L5, LOBLAWS_KEY),
        ("3101 X", "costco"),
    ],
    ids=["plu-not-in-the-file", "gtin", "in-store", "a-store-item-at-costco"],
)
def test_other_codes_miss(tmp_path: Path, raw_text: str, store_key: str) -> None:
    catalog = PluCatalog(_plu_file(tmp_path))

    assert catalog.lookup(_code(raw_text, store_key), store_key) is None


def test_a_missing_file_misses_with_one_warning(tmp_path: Path) -> None:
    path = tmp_path / "catalog" / "plu.csv"
    catalog = PluCatalog(path)

    with capture_logs() as logs:
        found = [catalog.lookup(_code(L3), LOBLAWS_KEY) for _ in range(3)]

    assert found == [None, None, None]
    assert _missing(logs) == [
        {
            "event": "catalog.missing",
            "log_level": "warning",
            "path": str(path),
            "reason": "no such file",
        }
    ]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"plu,name,organic\n3101,Test Apple,0\n", "no code column"),
        (b"code,title,organic\n3101,Test Apple,0\n", "no name column"),
        (b"code,name,organic\n3101,Test \xff\xfe Apple,0\n", "unreadable: UnicodeDecodeError"),
        (b"", "no header"),
    ],
    ids=["no-code-column", "no-name-column", "unreadable", "empty-file"],
)
def test_a_malformed_file_is_treated_as_missing(
    tmp_path: Path, content: bytes, reason: str
) -> None:
    path = tmp_path / "plu.csv"
    path.write_bytes(content)
    catalog = PluCatalog(path)

    with capture_logs() as logs:
        found = [catalog.lookup(_code(L3), LOBLAWS_KEY) for _ in range(2)]

    assert found == [None, None]
    assert [(entry["path"], entry["reason"]) for entry in _missing(logs)] == [(str(path), reason)]


def test_a_header_only_file_misses_silently(tmp_path: Path) -> None:
    catalog = PluCatalog(_plu_file(tmp_path, "code,name,organic\n"))

    with capture_logs() as logs:
        found = catalog.lookup(_code(L3), LOBLAWS_KEY)

    assert found is None
    assert _missing(logs) == []


def test_the_file_is_read_once(tmp_path: Path) -> None:
    path = _plu_file(tmp_path)
    catalog = PluCatalog(path)
    first = catalog.lookup(_code(L3), LOBLAWS_KEY)

    path.unlink()

    assert catalog.lookup(_code(L3), LOBLAWS_KEY) == first == TEST_APPLE


# --- CompositeCatalog ---------------------------------------------------------------------------

STORE_ENTRY = CatalogProduct(
    "store_catalog", "3101", 7, "Our Apple", None, None, None, Category.PRODUCE, ()
)


@pytest.mark.parametrize(
    ("store_entries", "plu_entries", "expected", "plu_calls"),
    [
        ({"3101": STORE_ENTRY}, {"3101": TEST_APPLE}, STORE_ENTRY, 0),
        ({}, {"3101": TEST_APPLE}, TEST_APPLE, 1),
        ({}, {}, None, 1),
    ],
    ids=["both-hit", "store-miss", "both-miss"],
)
def test_the_composite_asks_in_order_and_the_first_hit_wins(
    store_entries: dict[str, CatalogProduct],
    plu_entries: dict[str, CatalogProduct],
    expected: CatalogProduct | None,
    plu_calls: int,
) -> None:
    store, plu = FakeCatalog(store_entries), FakeCatalog(plu_entries)
    code = _code(L3)

    found = CompositeCatalog(store, plu).lookup(code, LOBLAWS_KEY)

    assert found == expected
    assert store.calls == [(code, LOBLAWS_KEY)]
    assert len(plu.calls) == plu_calls
