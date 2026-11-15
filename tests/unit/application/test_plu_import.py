"""``catalog plu-import``: ``plu.csv`` from an IFPS "Export all -> .csv" file (barcode-lookup plan
R17, A12-A15; PC-TC-122..124, 164..167).

Pinned (signatures "catalog plu-import", TQ36, TQ38-TQ41):
- ``import_plu(source, target) -> PluImport``: columns found by header (trimmed, any case;
  ``Plu``, ``Commodity``, ``Variety``, ``Size`` required, ``Category`` used when present), every
  cell trimmed, read as ``utf-8-sig``. Output ``code,name,organic`` sorted by code, ``organic``
  always 0, names CSV-quoted when they hold a comma, ``\\n`` line ends.
- One skip reason per record, checked retailer assigned -> invalid plu -> no commodity ->
  duplicate (the first importable record wins); ``PluImport.skipped`` holds only reasons that
  occurred, in ``PluSkip`` order. A blank line is no record.
- ``plu_name``: ``"<Commodity>[, <Variety>][, <size>]"``, commodity ``str.title()``, variety
  dropped when empty or exactly ``Retailer Assigned`` (any case), size lower-cased and dropped
  when empty or ``All Sizes`` (any case).
- Atomic: the old file is byte-identical and no other file is left on every error (missing
  column -> DomainValidationError naming it; missing source or nothing importable ->
  NotFoundError; a directory -> OSError; not UTF-8 -> UnicodeDecodeError; a failed rename).

Synthetic rows only: never a row of the real export.
"""

import csv
import io
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from paper_trail.adapters.catalog.plu import PluCatalog
from paper_trail.application.catalog import PluImport, PluSkip, import_plu, plu_name
from paper_trail.domain.errors import DomainValidationError, NotFoundError
from paper_trail.domain.rules.item_codes import ItemCode, parse_item_code

HEADER = ("id", "Plu", "Type", "Category", "Commodity", "Variety", "Size", "Notes")
RETAILER = "Retailer Assigned Numbers"


@dataclass(frozen=True, slots=True)
class Record:
    plu: str
    commodity: str = "TEST PEAR"
    variety: str = "Green"
    size: str = "Large"
    category: str = "Test Fruits"
    notes: str = ""

    def cells(self, index: int) -> dict[str, str]:
        return {
            "id": str(index),
            "Plu": self.plu,
            "Type": "Global",
            "Category": self.category,
            "Commodity": self.commodity,
            "Variety": self.variety,
            "Size": self.size,
            "Notes": self.notes,
        }


IFPS_RECORDS = (
    Record("3101", "TEST APPLE", "Red", " Small "),
    Record("3102", "TEST APPLE", "Red", " Large "),
    Record("4321", "SAMPLE KIWI", "Green (includes Demo) ", "  All Sizes  "),
    Record("3300", "TEST GRAPE", "Retailer Assigned", "All Sizes"),
    Record("3400", "ANY TEST COMMODITY", "Retailer Assigned", "All Sizes", RETAILER),
    Record("3102", "TEST APPLE", "Red", "Large"),
)
IMPORTED_CSV = (
    "code,name,organic\n"
    '3101,"Test Apple, Red, small",0\n'
    '3102,"Test Apple, Red, large",0\n'
    "3300,Test Grape,0\n"
    '4321,"Sample Kiwi, Green (includes Demo)",0\n'
)
IMPORTED_SKIPS = {PluSkip.RETAILER_ASSIGNED: 1, PluSkip.DUPLICATE: 1}
OLD_CSV = "code,name,organic\n4999,Old Name,0\n"


def ifps_csv(
    records: Sequence[Record] = IFPS_RECORDS,
    header: Sequence[str] = HEADER,
    *,
    rename: Mapping[str, str] | None = None,
    line_end: str = "\n",
) -> str:
    """The export's shape: ``header`` (written as ``rename`` says), one row per record, cells
    of columns a record does not know left empty."""
    names = rename or {}
    out = io.StringIO()
    writer = csv.writer(out, lineterminator=line_end)
    writer.writerow([names.get(column, column) for column in header])
    for index, record in enumerate(records, 1):
        cells = record.cells(index)
        writer.writerow([cells.get(column, "") for column in header])
    return out.getvalue()


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return tmp_path / "data" / "catalog" / "plu.csv"


def _source(tmp_path: Path, text: str, name: str = "export.csv") -> Path:
    source = tmp_path / "in" / name
    source.parent.mkdir(exist_ok=True)
    source.write_text(text, encoding="utf-8", newline="")
    return source


def _import(tmp_path: Path, target: Path, text: str) -> tuple[str, PluImport]:
    found = import_plu(_source(tmp_path, text), target)
    return target.read_text(encoding="utf-8"), found


def _expected(target: Path, imported: int, skipped: Mapping[PluSkip, int]) -> PluImport:
    return PluImport(path=target, imported=imported, skipped=dict(skipped))


def _code(raw_text: str) -> ItemCode:
    code = parse_item_code(raw_text, "loblaws")
    assert code is not None
    return code


# --- the whole file (PC-TC-122) -------------------------------------------------------------------


def test_pc_tc_122_the_export_becomes_the_normalized_file(tmp_path: Path, target: Path) -> None:
    written, found = _import(tmp_path, target, ifps_csv())

    assert (written, found) == (IMPORTED_CSV, _expected(target, 4, IMPORTED_SKIPS))
    assert list(found.skipped) == [PluSkip.RETAILER_ASSIGNED, PluSkip.DUPLICATE]


@pytest.mark.parametrize(
    ("raw_text", "name", "tags"),
    [
        ("3101 X", "Test Apple, Red, small", ()),
        ("93101 X", "Organic Test Apple, Red, small", ("organic",)),
        ("3300 X", "Test Grape", ()),
        ("3400 X", None, None),
    ],
)
def test_pc_tc_122_the_plu_catalog_reads_the_imported_file(
    tmp_path: Path, target: Path, raw_text: str, name: str | None, tags: tuple[str, ...] | None
) -> None:
    _import(tmp_path, target, ifps_csv())

    entry = PluCatalog(target).lookup(_code(raw_text), "loblaws")

    assert (None if entry is None else (entry.name, entry.tags)) == (
        None if name is None else (name, tags)
    )


# --- rows that name no product (PC-TC-123) --------------------------------------------------------

INVALID_ROWS = [
    ("7,abc,Global,Test Fruits,TEST PEAR,Green,Large,", PluSkip.INVALID_PLU),
    ("7,2999,Global,Test Fruits,TEST PEAR,Green,Large,", PluSkip.INVALID_PLU),
    ("7,5000,Global,Test Fruits,TEST PEAR,Green,Large,", PluSkip.INVALID_PLU),
    ("7,310,Global,Test Fruits,TEST PEAR,Green,Large,", PluSkip.INVALID_PLU),
    ("7,93101,Global,Test Fruits,TEST APPLE,Red,Small,", PluSkip.INVALID_PLU),
    ("7,,Global,Test Fruits,TEST PEAR,Green,Large,", PluSkip.INVALID_PLU),
    ("7,3500,Global,Test Fruits,   ,Green,Large,", PluSkip.NO_COMMODITY),
    ("7,3500,Global,Test Fruits", PluSkip.NO_COMMODITY),
    ("", None),
]


@pytest.mark.parametrize(
    ("line", "reason"),
    INVALID_ROWS,
    ids=["abc", "2999", "5000", "310", "organic 93101", "empty plu", "blank commodity",
         "short record", "blank line"],
)  # fmt: skip
def test_pc_tc_123_a_row_that_names_no_product_is_skipped_with_its_reason(
    tmp_path: Path, target: Path, line: str, reason: PluSkip | None
) -> None:
    extra = {} if reason is None else {reason: 1}
    skipped = {key: n for key in PluSkip if (n := {**IMPORTED_SKIPS, **extra}.get(key, 0))}

    written, found = _import(tmp_path, target, f"{ifps_csv()}{line}\n")

    assert (written, found) == (IMPORTED_CSV, _expected(target, 4, skipped))
    assert list(found.skipped) == list(skipped)


# --- atomic replace (PC-TC-124) -------------------------------------------------------------------


def _without(column: str) -> tuple[str, ...]:
    return tuple(name for name in HEADER if name != column)


def _fail_replace(*_: object) -> None:
    msg = "disk full"
    raise OSError(msg)


def _bad_source(tmp_path: Path, case: str) -> Path:
    """The source of one failing import of PC-TC-124."""
    if case.startswith("no "):
        return _source(tmp_path, ifps_csv(header=_without(case.removeprefix("no "))))
    texts = {
        "rename fails": ifps_csv(),
        "header only": ifps_csv(()),
        "every row skipped": ifps_csv(IFPS_RECORDS[4:5]),
    }
    if case in texts:
        return _source(tmp_path, texts[case])
    source = _source(tmp_path, "", "latin.csv")
    if case == "not utf-8":
        source.write_bytes(b"Plu,Commodity,Variety,Size\n3101,POMME \xe9,Red,Small\n")
        return source
    return source.parent if case == "directory" else source.with_name("missing.csv")


FAILED_IMPORTS = [
    ("no Plu", DomainValidationError, r"(?i)no plu column"),
    ("no Commodity", DomainValidationError, r"(?i)no commodity column"),
    ("no Variety", DomainValidationError, r"(?i)no variety column"),
    ("no Size", DomainValidationError, r"(?i)no size column"),
    ("absent", NotFoundError, r"does not exist"),
    ("directory", OSError, None),
    ("not utf-8", UnicodeDecodeError, None),
    ("rename fails", OSError, "disk full"),
    ("header only", NotFoundError, r"nothing to import"),
    ("every row skipped", NotFoundError, r"nothing to import"),
]


@pytest.mark.parametrize("failure", FAILED_IMPORTS, ids=[row[0] for row in FAILED_IMPORTS])
def test_pc_tc_124_a_failed_import_leaves_the_old_file_and_nothing_else(
    tmp_path: Path,
    target: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: tuple[str, type[Exception], str | None],
) -> None:
    case, error, message = failure
    target.parent.mkdir(parents=True)
    target.write_text(OLD_CSV, encoding="utf-8")
    source = _bad_source(tmp_path, case)
    if case == "rename fails":
        monkeypatch.setattr(os, "replace", _fail_replace)

    with pytest.raises(error, match=message):
        import_plu(source, target)

    assert target.read_bytes() == OLD_CSV.encode()
    assert sorted(path.name for path in target.parent.iterdir()) == ["plu.csv"]


def test_pc_tc_124_an_import_replaces_the_whole_file_the_same_way_twice(
    tmp_path: Path, target: Path
) -> None:
    target.parent.mkdir(parents=True)
    target.write_text(OLD_CSV, encoding="utf-8")
    source = _source(tmp_path, ifps_csv())

    first = import_plu(source, target)
    written = target.read_bytes()
    second = import_plu(source, target)

    assert written == IMPORTED_CSV.encode()
    assert target.read_bytes() == written
    assert first == second == _expected(target, 4, IMPORTED_SKIPS)
    assert sorted(path.name for path in target.parent.iterdir()) == ["plu.csv"]


def test_the_imported_file_is_world_readable(tmp_path: Path, target: Path) -> None:
    """``plu.csv`` is 0644 like the other data files, so a service under another UID can read
    it."""
    previous = os.umask(0o022)
    try:
        import_plu(_source(tmp_path, ifps_csv()), target)
    finally:
        os.umask(previous)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_pc_tc_124_a_catalog_loaded_before_the_import_keeps_its_table(
    tmp_path: Path, target: Path
) -> None:
    target.parent.mkdir(parents=True)
    target.write_text(OLD_CSV, encoding="utf-8")
    before = PluCatalog(target)
    old = before.lookup(_code("4999 X"), "loblaws")

    import_plu(_source(tmp_path, ifps_csv()), target)
    after = PluCatalog(target)

    names = [
        None if (entry := catalog.lookup(_code(raw), "loblaws")) is None else entry.name
        for catalog in (before, after)
        for raw in ("4999 X", "3101 X")
    ]
    assert old is not None
    assert names == ["Old Name", None, None, "Test Apple, Red, small"]


# --- headers and cells as the export writes them (PC-TC-164) --------------------------------------

EXTRA_COLUMNS = tuple(f"Extra {n}" for n in range(12))
PADDED = replace(IFPS_RECORDS[0], plu=" 3101 ", commodity="  TEST APPLE ", variety="Red  ")
MULTILINE = replace(IFPS_RECORDS[1], notes='a, b "c"\nd')
EXPORT_VARIATIONS = {
    "PLU": ifps_csv(rename={"Plu": "PLU"}),
    "plu": ifps_csv(rename={"Plu": "plu"}),
    " Plu ": ifps_csv(rename={"Plu": " Plu "}),
    "other cases": ifps_csv(
        rename={"Commodity": "COMMODITY", "Variety": " variety ", "Size": "SIZE"}
    ),
    "irrelevant columns anywhere": ifps_csv(
        header=("Notes", "Size", "id", "Variety", "Type", "Commodity", "Category", "Plu")
    ),
    "twelve more columns": ifps_csv(header=(*EXTRA_COLUMNS[:6], *HEADER, *EXTRA_COLUMNS[6:])),
    "quoted cell over two lines": ifps_csv((IFPS_RECORDS[0], MULTILINE, *IFPS_RECORDS[2:])),
    "crlf": ifps_csv(line_end="\r\n"),
    "byte-order mark": "﻿" + ifps_csv(header=("Plu", *_without("Plu"))),
    "padded cells": ifps_csv((PADDED, *IFPS_RECORDS[1:])),
}  # fmt: skip


@pytest.mark.parametrize("text", EXPORT_VARIATIONS.values(), ids=EXPORT_VARIATIONS.keys())
def test_pc_tc_164_headers_and_cells_are_read_as_the_export_writes_them(
    tmp_path: Path, target: Path, text: str
) -> None:
    assert _import(tmp_path, target, text) == (IMPORTED_CSV, _expected(target, 4, IMPORTED_SKIPS))


# --- the name (PC-TC-165) -------------------------------------------------------------------------

NAMES = [
    ("TEST APPLE", "Red", "Small", "Test Apple, Red, small"),
    ("TEST APPLE", "Red", "Extra Large", "Test Apple, Red, extra large"),
    ("TEST APPLE", "Red", "3-7 LBS", "Test Apple, Red, 3-7 lbs"),
    ("TEST APPLE", "Red", "Bulk 3-pack (3 Units)", "Test Apple, Red, bulk 3-pack (3 units)"),
    ("TEST APPLE", "Red", "All other sizes", "Test Apple, Red, all other sizes"),
    ("TEST APPLE", "Red", "All Sizes", "Test Apple, Red"),
    ("TEST APPLE", "Red", "All sizes", "Test Apple, Red"),
    ("TEST APPLE", "Red", "ALL SIZES", "Test Apple, Red"),
    ("TEST APPLE", "Red", "", "Test Apple, Red"),
    ("TEST APPLE", "", "Large", "Test Apple, large"),
    ("TEST APPLE", "", "All Sizes", "Test Apple"),
    ("TEST GRAPE", "Retailer Assigned", "All Sizes", "Test Grape"),
    ("TEST GRAPE", "retailer assigned", "All Sizes", "Test Grape"),
    ("TEST GRAPE", "RETAILER ASSIGNED", "All Sizes", "Test Grape"),
    ("TEST GRAPE", "Retailer Assigned", "Large", "Test Grape, large"),
    ("TEST MELON", "Retailer Assigned (includes Demo)", "",
     "Test Melon, Retailer Assigned (includes Demo)"),
    ("TEST APPLE", "Yellow (includes Demo)", "", "Test Apple, Yellow (includes Demo)"),
    ("TEST APPLE", 'red/GREEN "Mix", Demo', "", 'Test Apple, red/GREEN "Mix", Demo'),
    ("SAMPLE KIWI", "", "", "Sample Kiwi"),
    ("TEST PEAR/SAMPLE FRUIT (DEMO or OTHER)", "", "",
     "Test Pear/Sample Fruit (Demo Or Other)"),
    ("Test root(DEMO)", "", "", "Test Root(Demo)"),
    ("TÉST PEPPER", "", "", "Tést Pepper"),
]  # fmt: skip


@pytest.mark.parametrize(("commodity", "variety", "size", "expected"), NAMES)
def test_pc_tc_165_the_name_rule(commodity: str, variety: str, size: str, expected: str) -> None:
    assert plu_name(commodity, variety, size) == expected


# --- retailer-assigned blocks (PC-TC-166) ---------------------------------------------------------

APPLE = Record("3101", "TEST APPLE", "Red", "Small")
APPLE_CSV = '3101,"Test Apple, Red, small",0\n'
BLOCK = Record("3400", "ANY TEST COMMODITY", "Retailer Assigned", "All Sizes", RETAILER)
RETAILER_CASES = {
    "retailer block": ((APPLE, BLOCK), HEADER, APPLE_CSV, {PluSkip.RETAILER_ASSIGNED: 1}),
    "padded lower-case block": (
        (APPLE, replace(BLOCK, category=" retailer assigned numbers ")),
        HEADER,
        APPLE_CSV,
        {PluSkip.RETAILER_ASSIGNED: 1},
    ),
    "block with a real commodity and size": (
        (APPLE, replace(BLOCK, commodity="TEST PEAR", variety="Green", size="Large")),
        HEADER,
        APPLE_CSV,
        {PluSkip.RETAILER_ASSIGNED: 1},
    ),
    "no category column": (
        (APPLE, BLOCK), _without("Category"), f"{APPLE_CSV}3400,Any Test Commodity,0\n", {},
    ),
    "retailer variety": (
        (APPLE, IFPS_RECORDS[3]), HEADER, f"{APPLE_CSV}3300,Test Grape,0\n", {},
    ),
    "retailer variety, empty category": (
        (APPLE, replace(IFPS_RECORDS[3], category="")), HEADER,
        f"{APPLE_CSV}3300,Test Grape,0\n", {},
    ),
}  # fmt: skip


@pytest.mark.parametrize("case", RETAILER_CASES.values(), ids=RETAILER_CASES.keys())
def test_pc_tc_166_retailer_assigned_blocks_name_no_product(
    tmp_path: Path,
    target: Path,
    case: tuple[tuple[Record, ...], tuple[str, ...], str, dict[PluSkip, int]],
) -> None:
    records, header, rows, skipped = case
    written, found = _import(tmp_path, target, ifps_csv(records, header))

    expected_rows = rows.count("\n")
    assert (written, found) == (
        f"code,name,organic\n{rows}",
        _expected(target, expected_rows, skipped),
    )


# --- duplicates and the order of reasons (PC-TC-167) ----------------------------------------------

PEAR_3500 = Record("3500")
DUPLICATE_CASES = {
    "identical twice": ((PEAR_3500, PEAR_3500), {PluSkip.DUPLICATE: 1}),
    "other variety and size": (
        (PEAR_3500, replace(PEAR_3500, variety="Red", size="Small")),
        {PluSkip.DUPLICATE: 1},
    ),
    "three times": ((PEAR_3500, PEAR_3500, PEAR_3500), {PluSkip.DUPLICATE: 2}),
    "padded code": ((PEAR_3500, replace(PEAR_3500, plu=" 3500 ")), {PluSkip.DUPLICATE: 1}),
    "retailer block first": (
        (replace(PEAR_3500, category=RETAILER, commodity="ANY"), PEAR_3500),
        {PluSkip.RETAILER_ASSIGNED: 1},
    ),
    "no commodity first": (
        (replace(PEAR_3500, commodity=""), PEAR_3500),
        {PluSkip.NO_COMMODITY: 1},
    ),
    "retailer block second": (
        (PEAR_3500, replace(PEAR_3500, category=RETAILER, commodity="ANY")),
        {PluSkip.RETAILER_ASSIGNED: 1},
    ),
}


@pytest.mark.parametrize(
    ("records", "skipped"), DUPLICATE_CASES.values(), ids=DUPLICATE_CASES.keys()
)
def test_pc_tc_167_a_repeated_plu_keeps_its_first_importable_row(
    tmp_path: Path, target: Path, records: tuple[Record, ...], skipped: dict[PluSkip, int]
) -> None:
    written, found = _import(tmp_path, target, ifps_csv(records))

    assert (written, found) == (
        'code,name,organic\n3500,"Test Pear, Green, large",0\n',
        _expected(target, 1, skipped),
    )
