"""``paper-trail catalog todo|add|list|remove|lookup|plu-import`` end to end
(barcode-lookup plan R15-R18, A11-A16, Q3; PC-TC-104..169, CLI rows).

The CLI runs in-process through Typer's runner on real SQLite files under ``tmp_path``
(``PAPER_TRAIL_DATA_DIR``); state is stored through the real repositories. Receipts are
processed against a scripted fake Claude (Messages API) on 127.0.0.1 that answers the read only.

Pinned:
- Exit codes: 0 done (also an empty ``todo`` and every ``lookup``); 2 an unparsable code or
  size, neither or both of NAME and ``--product``, field options with ``--product``, a person's
  name without ``--replace`` (the message names the product and ends ``give --replace to
  rename it``), a missing option, an unknown ``--scheme``/``--category``/``--limit``, a missing
  ``plu-import`` column (named); 1 the main database is not migrated (``db upgrade``), an
  unknown ``--product`` or mapping, a declined prompt (``nothing saved``), an unreadable or
  empty export (``nothing to import``).
- ``todo --json`` keys: code, scheme, store_key, product_id, printed_name, times_bought,
  last_paid_cents, last_date, lookup_url. ``list --json`` keys: scheme, store_key, code,
  product_id, product_name, source.
- ``plu-import``'s last stdout line: ``N imported, M skipped (<n> <reason>, ...)`` in the order
  retailer assigned, duplicate, invalid plu, no commodity; ``N imported, 0 skipped`` alone.
- ``lookup`` prints the code's kind, its identity as ``<scheme> [<store>] <code>``, the source
  that hits (``store_catalog``/``plu``) and its name, else ``no source``; ``no code`` for a line
  without one. Table layouts are the implementer's: rows are checked for the facts they carry.
"""

import csv
import io
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from paper_trail.bootstrap import RUNS_DIRNAME, build_container, dispose
from paper_trail.config import DATABASE_FILENAME
from paper_trail.domain.enums import Category, CodeScheme, CodeSource, FieldSource, MatchSource
from paper_trail.domain.models import LineItem, Product, ProductCode
from paper_trail.interfaces.cli.main import app
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.e2e.agent_cli import (
    StartClaude,
    configure_cli,
    fake_claude_env,
    invoke,
    migrate_main,
    rows,
)
from tests.extractions import C1, C2, L1, L3, L4, LOBLAWS, PLU_CSV, STORE_NAME, U1, coded_answer
from tests.factories import CREATED_AT, discount_line, item_line, product
from tests.images import receipt_jpeg
from tests.storage import in_unit, seed_purchase
from tests.support import CopyMigrated, MakeSettings, require_id

RUN = "scratch"
EGGS = "1234567 KS ORG EGGS 24CT"
COSTCO_LINK = "https://www.costco.ca/CatalogSearch?keyword={}"
TABLES = (
    "receipts",
    "products",
    "product_codes",
    "product_aliases",
    "line_items",
    "price_observations",
    "corrections",
)
TODO_KEYS = (
    "code", "scheme", "store_key", "product_id", "printed_name", "times_bought",
    "last_paid_cents", "last_date", "lookup_url",
)  # fmt: skip
LIST_KEYS = ("scheme", "store_key", "code", "product_id", "product_name", "source")

IFPS_HEADER = ("id", "Plu", "Type", "Category", "Commodity", "Variety", "Size", "Notes")
IFPS_ROWS = {
    "apple-small": ("1", "3101", "Test", "Test Fruits", "TEST APPLE", "Red", " Small ", ""),
    "apple-large": ("2", "3102", "Test", "Test Fruits", "TEST APPLE", "Red", " Large ", ""),
    "kiwi": (
        "3", "4321", "Test", "Test Fruits", "SAMPLE KIWI", "Green (includes Demo) ",
        "  All Sizes  ", "",
    ),
    "grape": (
        "4", "3300", "Test", "Test Fruits", "TEST GRAPE", "Retailer Assigned", "All Sizes", "",
    ),
    "retailer": (
        "5", "3400", "Test", "Retailer Assigned Numbers", "ANY TEST COMMODITY",
        "Retailer Assigned", "All Sizes", "",
    ),
    "apple-large-again": ("6", "3102", "Test", "Test Fruits", "TEST APPLE", "Red", "Large", ""),
    "apple-small-again": ("7", "3101", "Test", "Test Fruits", "TEST APPLE", "Red", "Large", ""),
    "invalid": ("8", "abc", "Test", "Test Fruits", "TEST APPLE", "Red", "Small", ""),
    "no-commodity": ("9", "3200", "Test", "Test Fruits", "", "Red", "Small", ""),
}  # fmt: skip
IFPS = ("apple-small", "apple-large", "kiwi", "grape", "retailer", "apple-large-again")
IMPORTED_CSV = (
    "code,name,organic\n"
    '3101,"Test Apple, Red, small",0\n'
    '3102,"Test Apple, Red, large",0\n'
    "3300,Test Grape,0\n"
    '4321,"Sample Kiwi, Green (includes Demo)",0\n'
)

_runner = CliRunner()


@dataclass(frozen=True)
class Todo:
    """The TODO state's products (ids)."""

    choc: int
    kiwi: int
    eggs: int
    u1: int
    wipes: int
    apple: int


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return configure_cli(monkeypatch, tmp_path)


@pytest.fixture
def main_db(data_dir: Path, copy_migrated: CopyMigrated) -> Path:
    return copy_migrated(data_dir / DATABASE_FILENAME)


@pytest.fixture
def main_migrated(data_dir: Path) -> Path:
    """The main database at head: a ``--run`` needs it (its spend ledger, R3) and never
    migrates it itself (C1)."""
    migrate_main(data_dir)
    return data_dir


@pytest.fixture
def run_db(main_migrated: Path, copy_migrated: CopyMigrated) -> Path:
    """The run's database, migrated (the main one too)."""
    data_dir = main_migrated
    return copy_migrated(data_dir / RUNS_DIRNAME / RUN / DATABASE_FILENAME)


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartClaude]:
    with fake_claude_env(monkeypatch) as start:
        yield start


def _seeded[T](
    make_settings: MakeSettings, database: Path, seed: Callable[[UnitOfWorkFactory], T]
) -> T:
    container = build_container(make_settings(database_path=database))
    try:
        return seed(container.uow_factory)
    finally:
        dispose(container)


def _code(
    product_id: int, scheme: CodeScheme, store_key: str, code: str, source: CodeSource
) -> ProductCode:
    return ProductCode(None, product_id, scheme, store_key, code, source, CREATED_AT, CREATED_AT)


def _named(name: str, sources: dict[str, FieldSource], category: Category | None = None) -> Product:
    return product(
        canonical_name=name,
        brand=None,
        size_value=None,
        size_unit=None,
        category=category,
        tags=(),
        field_sources=sources,
    )


def _store_todo(factory: UnitOfWorkFactory) -> Todo:
    """TODO: at costco C1 bought 3 times (last 11-03, 1699 less a 400 discount), C2 once
    (11-01, 1599), ``1234567`` named by a person, U1 without a code; at loblaws L1 twice (last
    11-02, 899) and L3 named by the PLU table."""
    printed = {"canonical_name": FieldSource.RECEIPT_TEXT}
    plu = {"canonical_name": FieldSource.PLU, "category": FieldSource.PLU}

    def store(uow: UnitOfWork) -> Todo:
        def add(item: Product) -> int:
            return require_id(uow.products.add(item).id)

        todo = Todo(
            choc=add(_named("CHOC PISTACH", printed)),
            kiwi=add(_named("GOLD KIWI", printed)),
            eggs=add(_named("Kirkland Organic Eggs", {"canonical_name": FieldSource.PERSON})),
            u1=add(_named(U1, printed)),
            wipes=add(_named("PC UNS BABYWIPES", printed)),
            apple=add(_named("Test Apple", plu, Category.PRODUCE)),
        )
        for code in (
            _code(todo.choc, CodeScheme.STORE, "costco", "1362394", CodeSource.RECEIPT),
            _code(todo.kiwi, CodeScheme.STORE, "costco", "1543210", CodeSource.RECEIPT),
            _code(todo.eggs, CodeScheme.STORE, "costco", "1234567", CodeSource.MANUAL),
            _code(todo.wipes, CodeScheme.GTIN, "", "0060383064877", CodeSource.RECEIPT),
            _code(todo.apple, CodeScheme.PLU, "", "3101", CodeSource.PLU),
        ):
            uow.product_codes.upsert(code, replace=True)
        return todo

    todo = in_unit(factory, store)

    def line(position: int, text: str, cents: int, product_id: int) -> LineItem:
        return item_line(position, text, cents, product_id=product_id, match_source=MatchSource.NEW)

    seed_purchase(
        factory,
        [
            line(0, C1, 1699, todo.choc),
            line(1, C2, 1599, todo.kiwi),
            line(2, EGGS, 899, todo.eggs),
            line(3, U1, 899, todo.u1),
        ],
        purchased_on=date(2026, 11, 1),
    )
    seed_purchase(factory, [line(0, C1, 1699, todo.choc)], purchased_on=date(2026, 11, 2))
    seed_purchase(
        factory,
        [line(0, C1, 1699, todo.choc), discount_line(1, "TPD/1362394", -400, 0)],
        purchased_on=date(2026, 11, 3),
    )
    seed_purchase(
        factory,
        [line(0, L1, 899, todo.wipes), line(1, L3, 499, todo.apple)],
        store_name=LOBLAWS,
        purchased_on=date(2026, 11, 1),
    )
    seed_purchase(
        factory, [line(0, L1, 899, todo.wipes)], store_name=LOBLAWS, purchased_on=date(2026, 11, 2)
    )
    return todo


def _todo_json(todo: Todo) -> list[dict[str, Any]]:
    return [
        {
            "code": "1362394", "scheme": "store", "store_key": "costco",
            "product_id": todo.choc, "printed_name": "CHOC PISTACH", "times_bought": 3,
            "last_paid_cents": 1299, "last_date": "2026-11-03",
            "lookup_url": COSTCO_LINK.format("1362394"),
        },
        {
            "code": "0060383064877", "scheme": "gtin", "store_key": "", "product_id": todo.wipes,
            "printed_name": "PC UNS BABYWIPES", "times_bought": 2, "last_paid_cents": 899,
            "last_date": "2026-11-02", "lookup_url": None,
        },
        {
            "code": "1543210", "scheme": "store", "store_key": "costco",
            "product_id": todo.kiwi, "printed_name": "GOLD KIWI", "times_bought": 1,
            "last_paid_cents": 1599, "last_date": "2026-11-01",
            "lookup_url": COSTCO_LINK.format("1543210"),
        },
    ]  # fmt: skip


def _list_json(todo: Todo) -> list[dict[str, Any]]:
    return [
        dict(zip(LIST_KEYS, values, strict=True))
        for values in (
            ("gtin", "", "0060383064877", todo.wipes, "PC UNS BABYWIPES", "receipt"),
            ("plu", "", "3101", todo.apple, "Test Apple", "plu"),
            ("store", "costco", "1234567", todo.eggs, "Kirkland Organic Eggs", "manual"),
            ("store", "costco", "1362394", todo.choc, "CHOC PISTACH", "receipt"),
            ("store", "costco", "1543210", todo.kiwi, "GOLD KIWI", "receipt"),
        )
    ]


def _snapshot(database: Path) -> dict[str, list[tuple[Any, ...]]]:
    return {table: rows(database, f"SELECT * FROM {table} ORDER BY id") for table in TABLES}  # noqa: S608


def _catalog(*args: str, user_input: str | None = None) -> Result:
    result = _runner.invoke(app, ["catalog", *args], input=user_input)
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return result


def _json(result: Result) -> Any:
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _ifps(*names: str, header: tuple[str, ...] = IFPS_HEADER) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for name in names:
        cells = dict(zip(IFPS_HEADER, IFPS_ROWS[name], strict=True))
        writer.writerow([cells[column] for column in header])
    return buffer.getvalue()


def _export(tmp_path: Path, content: str | bytes, name: str = "export.csv") -> Path:
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _line_with(output: str, *facts: str) -> str:
    (line,) = [line for line in output.splitlines() if all(fact in line for fact in facts)]
    return line


def _process(claude: StartClaude, tmp_path: Path, *receipts: list[str], store: str) -> Any:
    """Process a photo per receipt (its printed texts) in ``RUN``; returns the fake Claude."""
    fake = claude(*[coded_answer(texts, store_name=store) for texts in receipts])
    for index, texts in enumerate(receipts):
        image = _export(tmp_path, receipt_jpeg([store, *texts]), f"{store}-{index}.jpg")
        result = invoke("process", "--no-color", "--run", RUN, str(image))
        assert result.exit_code == 0, result.output
    return fake


# --- todo (PC-TC-104, 105) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((), [0, 1, 2]),
        (("--store", STORE_NAME, "--limit", "1"), [0]),
    ],
    ids=["all", "store-name-and-limit"],
)
def test_pc_tc_104_105_todo_json_with_store_and_limit(
    main_db: Path, make_settings: MakeSettings, args: tuple[str, ...], expected: list[int]
) -> None:
    every = _todo_json(_seeded(make_settings, main_db, _store_todo))

    found = _json(_catalog("todo", "--json", *args))

    assert found == [every[index] for index in expected]
    assert all(tuple(row) == TODO_KEYS for row in found)


@pytest.mark.parametrize("limit", ["0", "-1", "x"])
def test_pc_tc_104_a_limit_below_1_or_not_a_number_is_exit_2(main_db: Path, limit: str) -> None:
    result = _catalog("todo", "--limit", limit)

    assert result.exit_code == 2, result.output


def test_pc_tc_105_the_table_shows_the_six_columns_in_order(
    main_db: Path, make_settings: MakeSettings
) -> None:
    _seeded(make_settings, main_db, _store_todo)

    result = _catalog("todo")

    assert result.exit_code == 0, result.output
    for facts in (
        ("1362394", "CHOC PISTACH", "3", "12.99", "2026-11-03", COSTCO_LINK.format("1362394")),
        ("0060383064877", "PC UNS BABYWIPES", "2", "8.99", "2026-11-02"),
        ("1543210", "GOLD KIWI", "1", "15.99", "2026-11-01", COSTCO_LINK.format("1543210")),
    ):
        line = _line_with(result.stdout, facts[0])
        rest = line
        for fact in facts:
            assert fact in rest, (fact, line)
            rest = rest[rest.index(fact) + len(fact) :]
    assert "costco.ca" not in _line_with(result.stdout, "0060383064877")


def test_pc_tc_105_an_empty_todo_is_one_line_or_an_empty_list(main_db: Path) -> None:
    table = _catalog("todo")

    assert table.exit_code == 0, table.output
    assert len([line for line in table.stdout.splitlines() if line.strip()]) == 1
    assert _json(_catalog("todo", "--json")) == []


# --- --run and the migrated database (PC-TC-107) ---------------------------------------------

RUN_COMMANDS = {
    "todo": ("todo",),
    "add": ("add", "costco", "1543210", "Gold Kiwi", "--yes"),
    "list": ("list",),
    "remove": ("remove", "--store", "costco", "--code", "1362394"),
    "lookup": ("lookup", C1, "--store", "costco"),
}


def _store_main_only(factory: UnitOfWorkFactory) -> None:
    """The main database's own data, unlike the run's: one printed-text product ``MAIN ONLY``
    with code ``7777777``, bought once."""
    pid = in_unit(
        factory,
        lambda uow: require_id(
            uow.products.add(_named("MAIN ONLY", {"canonical_name": FieldSource.RECEIPT_TEXT})).id
        ),
    )
    in_unit(
        factory,
        lambda uow: uow.product_codes.upsert(
            _code(pid, CodeScheme.STORE, "costco", "7777777", CodeSource.RECEIPT), replace=True
        ),
    )
    seed_purchase(
        factory,
        [item_line(0, "7777777 MAIN ONLY", 999, product_id=pid, match_source=MatchSource.NEW)],
    )


@pytest.mark.parametrize("command", RUN_COMMANDS)
def test_pc_tc_107_run_touches_only_that_runs_database(
    main_db: Path, run_db: Path, make_settings: MakeSettings, command: str
) -> None:
    """The two databases hold different data, so a command that read the main one would print
    ``7777777`` and miss ``1362394``."""
    _seeded(make_settings, main_db, _store_main_only)
    todo = _seeded(make_settings, run_db, _store_todo)
    main_bytes = main_db.read_bytes()

    result = _catalog(*RUN_COMMANDS[command], "--run", RUN)

    assert result.exit_code == 0, result.output
    assert main_db.read_bytes() == main_bytes
    if command == "add":
        assert rows(run_db, "SELECT canonical_name FROM products WHERE id = ?", todo.kiwi) == [
            ("Gold Kiwi",)
        ]
    elif command == "remove":
        assert rows(run_db, "SELECT count(*) FROM product_codes WHERE code = '1362394'") == [(0,)]
    elif command == "lookup":
        assert ("store_catalog" in result.stdout, "CHOC PISTACH" in result.stdout) == (True, True)
    else:
        assert ("1362394" in result.stdout, "7777777" in result.stdout) == (True, False)


@pytest.mark.parametrize("command", RUN_COMMANDS)
def test_pc_tc_107_without_run_the_main_database_must_be_migrated(
    data_dir: Path, command: str
) -> None:
    result = _catalog(*RUN_COMMANDS[command])

    assert result.exit_code == 1, result.output
    assert "db upgrade" in result.output
    assert not (data_dir / RUNS_DIRNAME / RUN).exists()


@pytest.mark.parametrize("configured", [False, True], ids=["default", "plu-path-setting"])
def test_pc_tc_107_plu_import_opens_no_database(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    target = data_dir / "catalog" / "plu.csv"
    if configured:
        target = tmp_path / "x" / "p.csv"
        monkeypatch.setenv("PAPER_TRAIL_CATALOG__PLU_PATH", str(target))

    result = _catalog("plu-import", str(_export(tmp_path, _ifps(*IFPS))))

    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8") == IMPORTED_CSV
    assert not (data_dir / DATABASE_FILENAME).exists()


# --- add (PC-TC-110, 113, 114, 116, 118, 153) ------------------------------------------------


@pytest.mark.parametrize(
    ("command", "store", "code"),
    [("add", "loblaws", "51234567890"), ("remove", "costco", "abc")],
    ids=["add-coupon", "remove-letters"],
)
def test_pc_tc_110_an_unparsable_code_is_exit_2_naming_it(
    main_db: Path, make_settings: MakeSettings, command: str, store: str, code: str
) -> None:
    _seeded(make_settings, main_db, _store_todo)
    before = _snapshot(main_db)
    args = (
        ("add", store, code, "Name", "--yes")
        if command == "add"
        else ("remove", "--store", store, "--code", code)
    )

    result = _catalog(*args)

    assert result.exit_code == 2, result.output
    assert "No such option" not in result.output
    assert repr(code) in result.output
    assert _snapshot(main_db) == before


def test_pc_tc_113_a_persons_name_needs_replace(main_db: Path, make_settings: MakeSettings) -> None:
    todo = _seeded(make_settings, main_db, _store_todo)
    before = _snapshot(main_db)

    refused = _catalog("add", "costco", "1234567", "Other Name", "--yes")
    after_refusal = _snapshot(main_db)
    same = _catalog("add", "costco", "1234567", "Kirkland Organic Eggs", "--yes")
    after_same = _snapshot(main_db)
    replaced = _catalog("add", "costco", "1234567", "Other Name", "--replace", "--yes")

    assert refused.exit_code == 2, refused.output
    for fact in (f"product {todo.eggs}", "Kirkland Organic Eggs", "give --replace to rename it"):
        assert fact in refused.output
    assert (after_refusal, after_same) == (before, before)
    assert (same.exit_code, replaced.exit_code) == (0, 0), replaced.output
    assert rows(main_db, "SELECT canonical_name FROM products WHERE id = ?", todo.eggs) == [
        ("Other Name",)
    ]


@pytest.mark.parametrize(
    "args",
    [
        ("add", "costco", "1543210", "--yes"),
        ("add", "costco", "1543210", "Name", "--product", "1", "--yes"),
        ("add", "costco", "1543210", "Name", "--size", "1.36 stone", "--yes"),
        ("add", "costco", "1543210", "--product", "1", "--brand", "B", "--yes"),
        ("add", "costco", "1543210", "", "--yes"),
        ("add", "costco", "1543210", "   ", "--yes"),
    ],
    ids=["neither", "both", "bad-size", "product-and-brand", "empty-name", "blank-name"],
)  # fmt: skip
def test_pc_tc_114_153_invalid_options_are_exit_2_and_write_nothing(
    main_db: Path, make_settings: MakeSettings, args: tuple[str, ...]
) -> None:
    _seeded(make_settings, main_db, _store_todo)
    before = _snapshot(main_db)

    result = _catalog(*args)

    assert result.exit_code == 2, result.output
    assert _snapshot(main_db) == before


@pytest.mark.parametrize(
    ("size", "value", "unit"),
    [("1.36kg", "1.36", "kg")],
)
def test_pc_tc_114_accepted_sizes(
    main_db: Path, make_settings: MakeSettings, size: str, value: str, unit: str
) -> None:
    todo = _seeded(make_settings, main_db, _store_todo)

    result = _catalog("add", "costco", "1543210", "Gold Kiwi", "--size", size, "--yes")

    assert result.exit_code == 0, result.output
    ((stored_value, stored_unit),) = rows(
        main_db, "SELECT size_value, size_unit FROM products WHERE id = ?", todo.kiwi
    )
    assert (Decimal(str(stored_value)), stored_unit) == (Decimal(value), unit)


def test_pc_tc_114_an_unknown_product_is_exit_1(main_db: Path, make_settings: MakeSettings) -> None:
    _seeded(make_settings, main_db, _store_todo)
    before = _snapshot(main_db)

    result = _catalog("add", "costco", "1543210", "--product", "999", "--yes")

    assert result.exit_code == 1, result.output
    assert _snapshot(main_db) == before


def test_pc_tc_116_without_yes_it_shows_before_and_after_and_saves_on_y(
    main_db: Path, make_settings: MakeSettings
) -> None:
    todo = _seeded(make_settings, main_db, _store_todo)

    result = _catalog("add", "costco", "1362394", "Choc Pistachio Bars", user_input="y\n")

    assert result.exit_code == 0, result.output
    for fact in ("1362394", "CHOC PISTACH", "Choc Pistachio Bars", "Save?"):
        assert fact in result.output
    assert rows(main_db, "SELECT canonical_name FROM products WHERE id = ?", todo.choc) == [
        ("Choc Pistachio Bars",)
    ]


@pytest.mark.parametrize("answer", ["n\n", ""], ids=["no", "end-of-input"])
def test_pc_tc_116_no_or_end_of_input_saves_nothing(
    main_db: Path, make_settings: MakeSettings, answer: str
) -> None:
    _seeded(make_settings, main_db, _store_todo)
    before = _snapshot(main_db)

    result = _catalog("add", "costco", "1362394", "Choc Pistachio Bars", user_input=answer)

    assert result.exit_code == 1, result.output
    assert "nothing saved" in result.output
    assert _snapshot(main_db) == before


def test_pc_tc_116_yes_asks_nothing(main_db: Path, make_settings: MakeSettings) -> None:
    _seeded(make_settings, main_db, _store_todo)

    result = _catalog("add", "costco", "1362394", "Choc Pistachio Bars", "--yes")

    assert result.exit_code == 0, result.output
    assert "Save?" not in result.output


@pytest.mark.usefixtures("main_migrated")
def test_pc_tc_118_after_add_a_receipt_links_to_the_named_product(
    data_dir: Path, claude: StartClaude, tmp_path: Path
) -> None:
    added = _catalog("add", "costco", "1543210", "Gold Kiwi 1 kg", "--yes", "--run", RUN)
    assert added.exit_code == 0, added.output
    database = data_dir / RUNS_DIRNAME / RUN / DATABASE_FILENAME
    ((named,),) = rows(database, "SELECT id FROM products")

    _process(claude, tmp_path, [C2], store=STORE_NAME)

    assert rows(database, "SELECT product_id, match_source FROM line_items") == [(named, "catalog")]
    assert rows(database, "SELECT count(*) FROM products") == [(1,)]


# --- list, remove, lookup (PC-TC-119, 120, 121) ----------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [((), [0, 1, 2, 3, 4]), (("--store", "costco"), [2, 3, 4]), (("--scheme", "plu"), [1])],
    ids=["all", "store", "scheme"],
)
def test_pc_tc_119_list_json_and_filters(
    main_db: Path, make_settings: MakeSettings, args: tuple[str, ...], expected: list[int]
) -> None:
    every = _list_json(_seeded(make_settings, main_db, _store_todo))

    found = _json(_catalog("list", "--json", *args))

    assert found == [every[index] for index in expected]
    assert all(tuple(row) == LIST_KEYS for row in found)


def test_pc_tc_119_the_table_and_an_unknown_scheme(
    main_db: Path, make_settings: MakeSettings
) -> None:
    todo = _seeded(make_settings, main_db, _store_todo)

    table = _catalog("list")
    upc = _catalog("list", "--scheme", "upc")

    assert table.exit_code == 0, table.output
    codes = ["0060383064877", "3101", "1234567", "1362394", "1543210"]
    lines = [_line_with(table.stdout, code) for code in codes]
    assert lines == sorted(lines, key=table.stdout.index)
    assert "CHOC PISTACH" in lines[3]
    assert str(todo.choc) in lines[3]
    assert upc.exit_code == 2, upc.output


@pytest.mark.parametrize(
    ("store", "code", "gone"),
    [("loblaws", "06038306487", "0060383064877")],
    ids=["bare-gtin"],
)
def test_pc_tc_120_remove_deletes_the_mapping_and_keeps_the_product(
    main_db: Path, make_settings: MakeSettings, store: str, code: str, gone: str
) -> None:
    _seeded(make_settings, main_db, _store_todo)
    before = _snapshot(main_db)

    result = _catalog("remove", "--store", store, "--code", code)

    assert result.exit_code == 0, result.output
    after = _snapshot(main_db)
    assert [row for row in before["product_codes"] if gone not in row] == after["product_codes"]
    assert {**after, "product_codes": []} == {**before, "product_codes": []}


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--store", "costco", "--code", "1100231"), 1),
        (("--store", "loblaws", "--code", "1362394"), 1),
        (("--code", "1362394"), 2),
        (("--store", "costco"), 2),
    ],
    ids=["unknown", "other-store", "no-store", "no-code"],
)
def test_pc_tc_120_remove_refusals(
    main_db: Path, make_settings: MakeSettings, args: tuple[str, ...], code: int
) -> None:
    _seeded(make_settings, main_db, _store_todo)
    before = _snapshot(main_db)

    result = _catalog("remove", *args)

    assert result.exit_code == code, result.output
    assert _snapshot(main_db) == before


def test_pc_tc_120_after_remove_the_next_receipt_makes_a_new_product(
    run_db: Path, make_settings: MakeSettings, claude: StartClaude, tmp_path: Path
) -> None:
    todo = _seeded(make_settings, run_db, _store_todo)
    removed = _catalog("remove", "--store", "costco", "--code", "1362394", "--run", RUN)
    assert removed.exit_code == 0, removed.output

    _process(claude, tmp_path, [C1], store=STORE_NAME)

    ((new_id, name, sources),) = rows(
        run_db, "SELECT id, canonical_name, field_sources FROM products WHERE id > ?", todo.apple
    )
    assert (name, json.loads(sources)) == ("CHOC PISTACH", {"canonical_name": "receipt_text"})
    assert rows(run_db, "SELECT product_id, source FROM product_codes WHERE code = '1362394'") == [
        (new_id, "receipt")
    ]
    assert rows(run_db, "SELECT count(*) FROM line_items WHERE product_id = ?", todo.choc) == [(3,)]


LOOKUPS = [
    (C1, "costco", False, ("store_item", "store costco 1362394", "store_catalog", "CHOC PISTACH")),
    (L3, "loblaws", False, ("plu", "plu 3101", "Test Apple")),
    (L3, "loblaws", True, ("plu", "plu 3101", "store_catalog")),
    (L4, "loblaws", False, ("plu", "plu 93101", "3101", "Organic Test Apple")),
    ("4999 X", "loblaws", False, ("plu", "plu 4999", "no source")),
    (L1, "loblaws", False, ("gtin", "gtin 0060383064877", "no source")),
    ("51234567890 COUPON", "loblaws", False, ("none", "no source")),
    (U1, "loblaws", False, ("no code",)),
]  # fmt: skip


@pytest.mark.parametrize(
    ("raw_text", "store", "ours", "facts"),
    LOOKUPS,
    ids=["c1-ours", "l3-plu", "l3-ours-first", "l4-organic", "plu-missing", "l1-unknown",
         "coupon", "u1"],
)  # fmt: skip
def test_pc_tc_121_lookup_explains_and_writes_nothing(  # noqa: PLR0913, PLR0917 - one row of the table
    main_db: Path,
    data_dir: Path,
    make_settings: MakeSettings,
    raw_text: str,
    store: str,
    ours: bool,
    facts: tuple[str, ...],
) -> None:
    (data_dir / "catalog").mkdir()
    _export(data_dir, PLU_CSV, "catalog/plu.csv")
    choc = _seeded(make_settings, main_db, _store_choc)
    if ours:
        _seeded(make_settings, main_db, _store_apple_code)
    before = _snapshot(main_db)

    result = _catalog("lookup", raw_text, "--store", store)

    assert result.exit_code == 0, result.output
    for fact in facts:
        assert fact in result.stdout, result.stdout
    if raw_text == C1:
        assert str(choc) in result.stdout
    assert _snapshot(main_db) == before


def _store_choc(factory: UnitOfWorkFactory) -> int:
    def store(uow: UnitOfWork) -> int:
        choc = require_id(
            uow.products.add(
                _named("CHOC PISTACH", {"canonical_name": FieldSource.RECEIPT_TEXT})
            ).id
        )
        uow.product_codes.upsert(
            _code(choc, CodeScheme.STORE, "costco", "1362394", CodeSource.RECEIPT), replace=True
        )
        return choc

    return in_unit(factory, store)


def _store_apple_code(factory: UnitOfWorkFactory) -> None:
    def store(uow: UnitOfWork) -> None:
        apple = require_id(uow.products.add(_named("Our Apple", {})).id)
        uow.product_codes.upsert(
            _code(apple, CodeScheme.PLU, "", "3101", CodeSource.MANUAL), replace=True
        )

    in_unit(factory, store)


def test_pc_tc_121_a_bare_code_reads_as_code_x(main_db: Path, data_dir: Path) -> None:
    (data_dir / "catalog").mkdir()
    _export(data_dir, PLU_CSV, "catalog/plu.csv")

    bare = _catalog("lookup", "3101", "--store", "loblaws")
    with_text = _catalog("lookup", "3101 X", "--store", "loblaws")

    assert (bare.exit_code, with_text.exit_code) == (0, 0), bare.output
    assert bare.stdout == with_text.stdout


def test_pc_tc_121_without_plu_csv_a_plu_has_no_source_and_one_warning(main_db: Path) -> None:
    result = _catalog("lookup", L3, "--store", "loblaws")

    assert result.exit_code == 0, result.output
    assert "no source" in result.stdout
    assert result.stderr.count('"catalog.missing"') == 1


# --- plu-import (PC-TC-124, 168, 169) --------------------------------------------------------


OVERSIZED_FIELD = "Plu,Commodity,Variety,Size\n3101,TEST APPLE," + "x" * 200_000 + ",Small\n"


def _without(column: str) -> str:
    return _ifps(*IFPS, header=tuple(name for name in IFPS_HEADER if name != column))


@pytest.mark.parametrize(
    ("make_export", "code", "message"),
    [
        (lambda tmp: _export(tmp, _without("Commodity")), 2, "commodity"),
        (lambda tmp: tmp / "missing.csv", 1, None),
        (lambda tmp: _export(tmp, _ifps()), 1, "nothing to import"),
        (lambda tmp: _export(tmp, OVERSIZED_FIELD), 1, "field"),
    ],
    ids=["missing-column", "absent", "nothing-to-import", "oversized-field"],
)
def test_pc_tc_124_a_refused_import_keeps_the_old_file(
    data_dir: Path,
    tmp_path: Path,
    make_export: Callable[[Path], Path],
    code: int,
    message: str | None,
) -> None:
    catalog_dir = data_dir / "catalog"
    catalog_dir.mkdir(parents=True)
    _export(catalog_dir, PLU_CSV, "plu.csv")

    result = _catalog("plu-import", str(make_export(tmp_path)))

    assert result.exit_code == code, result.output
    if message is not None:
        assert message in result.output.lower()
    assert "Traceback" not in result.output
    assert (catalog_dir / "plu.csv").read_text(encoding="utf-8") == PLU_CSV
    assert sorted(path.name for path in catalog_dir.iterdir()) == ["plu.csv"]


@pytest.mark.parametrize(
    ("names", "summary"),
    [
        (IFPS, "4 imported, 2 skipped (1 retailer assigned, 1 duplicate)"),
        (("apple-small", "kiwi"), "2 imported, 0 skipped"),
        (("apple-small", "apple-large", "kiwi", "apple-large-again"),
         "3 imported, 1 skipped (1 duplicate)"),
        (("apple-small", "retailer", "apple-small-again", "invalid", "no-commodity"),
         "1 imported, 4 skipped (1 retailer assigned, 1 duplicate, 1 invalid plu,"
         " 1 no commodity)"),
    ],
    ids=["ifps", "none-skipped", "one-reason", "all-reasons"],
)  # fmt: skip
def test_pc_tc_168_the_summary_line(
    data_dir: Path, tmp_path: Path, names: tuple[str, ...], summary: str
) -> None:
    result = _catalog("plu-import", str(_export(tmp_path, _ifps(*names))))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == summary
    imported = int(summary.split(maxsplit=1)[0])
    data_rows = (data_dir / "catalog" / "plu.csv").read_text(encoding="utf-8").splitlines()[1:]
    assert len(data_rows) == imported
    assert imported + int(summary.split()[2]) == len(names)


@pytest.mark.usefixtures("main_migrated")
def test_pc_tc_169_after_an_import_lookups_and_receipts_use_the_names(
    data_dir: Path,
    tmp_path: Path,
    claude: StartClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_GRAPH__ENRICHMENT", "true")
    imported = _catalog("plu-import", str(_export(tmp_path, _ifps(*IFPS))))
    assert imported.exit_code == 0, imported.output

    for code, facts in (
        ("3101", ("plu 3101", "plu", "Test Apple, Red, small")),
        ("93101", ("plu 93101", "Organic Test Apple, Red, small")),
        ("3300", ("Test Grape",)),
        ("3400", ("plu", "no source")),
    ):
        result = _catalog("lookup", code, "--store", "loblaws", "--run", RUN)
        assert result.exit_code == 0, result.output
        for fact in facts:
            assert fact in result.stdout, (code, result.stdout)

    fake = _process(claude, tmp_path, [L3], store=LOBLAWS)

    database = data_dir / RUNS_DIRNAME / RUN / DATABASE_FILENAME
    ((name, category, sources),) = rows(
        database, "SELECT canonical_name, category, field_sources FROM products"
    )
    assert (name, category, json.loads(sources)["canonical_name"]) == (
        "Test Apple, Red, small",
        "produce",
        "plu",
    )
    assert fake.calls == 1
