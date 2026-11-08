"""No card number reaches the database file or the logs (S1-AC-21, ADR-0011)."""

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Engine

from paper_trail.application.receipts import RecordReceipt
from paper_trail.domain.rules.arithmetic import Tolerances
from paper_trail.observability import configure_logging
from tests.factories import TEST_PAN, receipt_draft
from tests.fakes.clock import FakeClock
from tests.pans import card_numbers
from tests.storage import sqlite_uow_factory, table_values
from tests.support import MakeSettings

PAN_DIGITS = TEST_PAN.replace(" ", "")


def test_s1_ac_21_no_card_number_in_any_table_or_log_line(
    migrated_engine: Engine, make_settings: MakeSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    """S1-AC-21: after recording a draft carrying 4111 1111 1111 1111 in its store name,
    ocr_text, raw_text, description and product name, no cell of any table (FTS shadow tables
    included) and no log line holds a Luhn-valid 13-19 digit run."""
    configure_logging(make_settings(log_level="DEBUG"))
    draft = receipt_draft()
    first, second = draft.items
    draft = replace(
        draft,
        store_name=f"COSTCO WHOLESALE {PAN_DIGITS}",
        product_names={0: f"Organic Eggs {TEST_PAN}"},
        ocr_text=f"{draft.ocr_text}\nVISA {TEST_PAN}\nAUTH 123456",
        items=(
            replace(first, raw_text=f"{first.raw_text} {TEST_PAN}"),
            replace(second, description=f"Paid with {PAN_DIGITS}"),
        ),
    )
    record = RecordReceipt(
        sqlite_uow_factory(migrated_engine, FakeClock()), FakeClock(), Tolerances()
    )

    result = record(draft)

    assert result.masked_pans == 5
    database = Path(str(migrated_engine.url.database))
    leaks = [
        (table, column, value)
        for table, column, value in table_values(database)
        if card_numbers(value)
    ]
    assert leaks == []
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert card_numbers(stream) == []
        assert PAN_DIGITS not in stream.replace(" ", "")
    assert '"receipt.recorded"' in captured.out
