"""``paper-trail prices backfill [--run NAME] [--receipt ID] [--apply]`` end to end
(discounts-and-paid-prices plan, R9, Q11, Q26, Q27).

The CLI runs in-process through Typer's runner on a real SQLite file. The receipts are stored
the way they were before the feature (no ``applies_to_position``, observations at discount 0,
as migration 0004 leaves existing data), through the real repositories.

Pinned: a dry run by default (one row per discount line, nothing written); ``--apply`` writes
the links and the observations' discounts, and a second run reports ``0 changes``. Exit codes:
2 a non-integer ``--receipt``, an invalid ``--run``, an unknown receipt ("unknown receipt") or
one that is not COMPLETED ("is not completed"); 1 the main database is not migrated (the
``db upgrade`` hint). The row wording is the implementer's (Q26): rows are checked for the
facts they carry (the discount, its item, the regular and paid prices).
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import Result

from paper_trail.bootstrap import RUNS_DIRNAME, build_container, dispose
from paper_trail.config import DATABASE_FILENAME
from paper_trail.domain.enums import ProcessingStage, ReceiptStatus
from paper_trail.domain.models import LineItem, PriceObservation
from tests.e2e.agent_cli import configure_cli, invoke, rows, run_database
from tests.factories import (
    SIMPLE_SUBTOTAL,
    SIMPLE_TAX,
    SIMPLE_TOTAL,
    cad,
    price_observation,
    product,
    receipt,
    simple_lines,
)
from tests.support import CopyMigrated, MakeSettings, require_id

RUN = "exp1"
EGGS = "1234567 KS ORG EGGS 24CT"
LINKS = "SELECT receipt_id, position, applies_to_position FROM line_items ORDER BY id"
OBSERVATIONS = (
    "SELECT receipt_id, line_item_id, unit_price_cents, line_total_cents, discount_cents,"
    " unit_discount_cents, paid_cents, on_sale FROM price_observations ORDER BY id"
)


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return configure_cli(monkeypatch, tmp_path)


@pytest.fixture
def main_db(data_dir: Path, copy_migrated: CopyMigrated) -> Path:
    return copy_migrated(data_dir / DATABASE_FILENAME)


def _store_pre_feature(
    make_settings: MakeSettings,
    database: Path,
    status: ReceiptStatus = ReceiptStatus.COMPLETED,
) -> int:
    """SIMPLE stored as before the feature: line 4 unlinked; when COMPLETED, lines 0-3 matched
    with an observation each at discount 0. Returns the receipt id."""
    container = build_container(make_settings(database_path=database))
    try:
        with container.uow_factory() as uow:
            lines = simple_lines()
            if status is ReceiptStatus.COMPLETED:
                ids = [
                    require_id(uow.products.add(product(canonical_name=line.description)).id)
                    for line in lines[:4]
                ]
                matched = zip(lines[:4], ids, strict=True)
                lines = (*(replace(line, product_id=pid) for line, pid in matched), lines[4])
            stage = (
                ProcessingStage.DONE
                if status is ReceiptStatus.COMPLETED
                else ProcessingStage.NEEDS_REVIEW
            )
            stored = uow.receipts.add(
                receipt(
                    status=status,
                    stage=stage,
                    items=lines,
                    subtotal=cad(SIMPLE_SUBTOTAL),
                    tax=cad(SIMPLE_TAX),
                    total=cad(SIMPLE_TOTAL),
                )
            )
            receipt_id = require_id(stored.id)
            if status is ReceiptStatus.COMPLETED:
                uow.prices.add_many([_observation(receipt_id, line) for line in stored.items[:4]])
            uow.commit()
    finally:
        dispose(container)
    return receipt_id


def _observation(receipt_id: int, line: LineItem) -> PriceObservation:
    return price_observation(
        product_id=require_id(line.product_id),
        receipt_id=receipt_id,
        line_item_id=require_id(line.id),
        quantity=line.quantity,
        unit_price=line.unit_price,
        line_total=line.line_total,
    )


def _snapshot(database: Path) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    return rows(database, LINKS), rows(database, OBSERVATIONS)


def _backfill(*args: str) -> Result:
    result = invoke("prices", "backfill", *args)
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return result


def _line_with(output: str, text: str) -> str:
    (line,) = [line for line in output.splitlines() if text in line]
    return line


def _pre_feature_rows(receipt_id: int, first_line_id: int) -> list[tuple[Any, ...]]:
    """The four observations of a pre-feature SIMPLE: regular prices, discount 0."""
    regular = [(899, 899), (329, 658), (150, 204), (1979, 1979)]
    return [
        (receipt_id, first_line_id + offset, unit, total, 0, 0, total, 0)
        for offset, (unit, total) in enumerate(regular)
    ]


def test_tc_101_a_dry_run_lists_the_discount_and_writes_nothing(
    main_db: Path, make_settings: MakeSettings
) -> None:
    _store_pre_feature(make_settings, main_db)
    before = _snapshot(main_db)

    result = _backfill()

    assert result.exit_code == 0, result.output
    row = _line_with(result.stdout, "TPD/1234567")
    for fact in (EGGS, "-5.00", "8.99", "3.99"):
        assert fact in row, row
    assert "dry run" in result.stdout.lower()
    assert _snapshot(main_db) == before


def test_tc_102_apply_links_and_prices_then_a_second_run_changes_nothing(
    main_db: Path, make_settings: MakeSettings
) -> None:
    receipt_id = _store_pre_feature(make_settings, main_db)

    first = _backfill("--apply")

    assert first.exit_code == 0, first.output
    assert "2 changes" in first.stdout  # 1 link set + 1 observation updated
    assert rows(main_db, LINKS) == [(receipt_id, p, 0 if p == 4 else None) for p in range(5)]
    assert rows(main_db, OBSERVATIONS) == [
        (receipt_id, 1, 899, 899, -500, -500, 399, 1),
        *_pre_feature_rows(receipt_id, 1)[1:],
    ]
    after_first = _snapshot(main_db)

    second = _backfill("--apply")

    assert second.exit_code == 0, second.output
    assert "0 changes" in second.stdout
    assert _snapshot(main_db) == after_first


def test_tc_103_run_and_receipt_change_only_that_receipt_of_that_run(
    main_db: Path, data_dir: Path, make_settings: MakeSettings, copy_migrated: CopyMigrated
) -> None:
    run_db = copy_migrated(data_dir / RUNS_DIRNAME / RUN / DATABASE_FILENAME)
    assert run_db == run_database(data_dir, RUN)
    _store_pre_feature(make_settings, main_db)
    other = _store_pre_feature(make_settings, run_db)
    chosen = _store_pre_feature(make_settings, run_db)
    main_before = _snapshot(main_db)

    result = _backfill("--run", RUN, "--receipt", str(chosen), "--apply")

    assert result.exit_code == 0, result.output
    assert rows(run_db, LINKS) == [
        *[(other, p, None) for p in range(5)],
        *[(chosen, p, 0 if p == 4 else None) for p in range(5)],
    ]
    assert rows(run_db, OBSERVATIONS) == [
        *_pre_feature_rows(other, 1),
        (chosen, 6, 899, 899, -500, -500, 399, 1),
        *_pre_feature_rows(chosen, 6)[1:],
    ]
    assert _snapshot(main_db) == main_before


BACKFILL_EXIT_CODES = [
    (("--receipt", "abc"), 2, None),
    (("--run", "bad/name"), 2, None),
    (("--receipt", "999"), 2, "unknown receipt"),
    (("--receipt", "0"), 2, "unknown receipt"),
    (("--receipt", "2"), 2, "receipt 2 is not completed"),  # Q27: receipt 2 needs review
]


@pytest.mark.parametrize(
    ("args", "code", "message"),
    BACKFILL_EXIT_CODES,
    ids=["non-integer", "bad-run", "unknown", "zero", "not-completed"],
)
def test_tc_104_bad_arguments_exit_2_and_change_nothing(
    main_db: Path,
    make_settings: MakeSettings,
    args: tuple[str, ...],
    code: int,
    message: str | None,
) -> None:
    _store_pre_feature(make_settings, main_db)
    _store_pre_feature(make_settings, main_db, ReceiptStatus.NEEDS_REVIEW)
    before = _snapshot(main_db)

    result = _backfill(*args, "--apply")

    assert result.exit_code == code, result.output
    if message is not None:
        assert message in result.output
    assert "Traceback" not in result.output
    assert _snapshot(main_db) == before


def test_tc_104_the_main_database_must_be_migrated(data_dir: Path) -> None:
    result = _backfill()

    assert result.exit_code == 1, result.output
    assert "db upgrade" in result.output
