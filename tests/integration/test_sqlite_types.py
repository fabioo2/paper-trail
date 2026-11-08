"""Column types that keep Decimal, dates, times and enums exact in SQLite (S1-AC-11, PLAN 3.11).

Pinned (docs/tests/stage1.md): UtcDateTime stores fixed-width ISO-8601 text in UTC with
microseconds ("2026-01-01T12:00:00.000000+00:00") so text order is time order, and rejects naive
datetimes; DecimalText stores ``str(Decimal)`` and rejects floats; StrEnumType stores the value.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Column, Engine, Integer, MetaData, Table, insert, select, text
from sqlalchemy.exc import StatementError

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.types import DecimalText, IsoDate, StrEnumType, UtcDateTime
from paper_trail.domain.enums import ReceiptStatus
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import line_item, receipt
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, sqlite_uow_factory

metadata = MetaData()
samples = Table(
    "samples",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("at", UtcDateTime(), nullable=True),
    Column("on", IsoDate(), nullable=True),
    Column("amount", DecimalText(), nullable=True),
    Column("status", StrEnumType(ReceiptStatus), nullable=True),
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(tmp_path / "types.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def _round_trip(engine: Engine, **values: Any) -> tuple[Any, ...]:
    with engine.begin() as connection:
        key = connection.execute(insert(samples).values(**values)).inserted_primary_key
        assert key is not None
        row_id = key[0]
        loaded = connection.execute(select(samples).where(samples.c.id == row_id)).one()
        raw = connection.execute(
            text('SELECT "at", "on", amount, status, typeof(amount) FROM samples WHERE id = :id'),
            {"id": row_id},
        ).one()
    return tuple(loaded), tuple(raw)


def test_s1_ac_11_utc_datetime_is_fixed_width_utc_text(engine: Engine) -> None:
    """Pinned: microseconds are always written, offset is +00:00, values come back aware."""
    loaded, raw = _round_trip(engine, at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    assert raw[0] == "2026-01-01T12:00:00.000000+00:00"
    assert loaded[1] == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert loaded[1].utcoffset() == timedelta(0)


def test_s1_ac_11_utc_datetime_converts_other_offsets_to_utc(engine: Engine) -> None:
    """Pinned: an aware non-UTC time is stored as the same instant in UTC."""
    eastern = timezone(timedelta(hours=-5))

    loaded, raw = _round_trip(engine, at=datetime(2026, 11, 6, 9, 30, 0, 250, tzinfo=eastern))

    assert raw[0] == "2026-11-06T14:30:00.000250+00:00"
    assert loaded[1] == datetime(2026, 11, 6, 14, 30, 0, 250, tzinfo=UTC)
    assert loaded[1].tzinfo is not None
    assert loaded[1].utcoffset() == timedelta(0)


def test_s1_ac_11_utc_datetime_rejects_naive_values(engine: Engine) -> None:
    """Pinned: a naive datetime is a programming error, never silently assumed UTC."""
    with pytest.raises(StatementError):
        _round_trip(engine, at=datetime(2026, 1, 1, 12, 0))  # noqa: DTZ001


def test_s1_ac_11_iso_date(engine: Engine) -> None:
    """Dates are stored as ISO text and come back as date objects."""
    loaded, raw = _round_trip(engine, on=date(2024, 2, 29))

    assert raw[1] == "2024-02-29"
    assert loaded[2] == date(2024, 2, 29)


@pytest.mark.parametrize("value", ["0.452", "1", "12.000", "0.1", "123456789.123456789"])
def test_s1_ac_11_decimal_text_is_exact(engine: Engine, value: str) -> None:
    """S1-AC-11 / PLAN 3.11: Decimal goes in and out as text, digit for digit."""
    loaded, raw = _round_trip(engine, amount=Decimal(value))

    assert raw[2] == value
    assert raw[4] == "text"
    assert isinstance(loaded[3], Decimal)
    assert str(loaded[3]) == value


def test_s1_ac_11_decimal_text_rejects_floats(engine: Engine) -> None:
    """Pinned: binding a float would smuggle binary rounding in; it is refused."""
    with pytest.raises(StatementError):
        _round_trip(engine, amount=0.1)


def test_s1_ac_11_str_enum_type(engine: Engine) -> None:
    """StrEnumType stores the lowercase value and returns the enum member."""
    loaded, raw = _round_trip(engine, status=ReceiptStatus.NEEDS_REVIEW)

    assert raw[3] == "needs_review"
    assert loaded[4] is ReceiptStatus.NEEDS_REVIEW


def test_s1_ac_11_str_enum_type_rejects_other_values(engine: Engine) -> None:
    """A plain string or another enum's member is refused, never stored unchecked."""
    with pytest.raises(StatementError):
        _round_trip(engine, status="completed")


def test_s1_ac_11_nulls_pass_through(engine: Engine) -> None:
    """Every type maps NULL to None and back."""
    loaded, _ = _round_trip(engine, at=None, on=None, amount=None, status=None)

    assert loaded[1:] == (None, None, None, None)


def test_s1_ac_11_repository_stores_quantity_as_text(migrated_engine: Engine) -> None:
    """S1-AC-11: line_items.quantity holds the Decimal's text, never a REAL."""
    uow_factory: UnitOfWorkFactory = sqlite_uow_factory(migrated_engine, FakeClock())
    items = (line_item(quantity=Decimal("0.452")),)
    in_unit(uow_factory, lambda uow: uow.receipts.add(receipt(items=items)))

    with migrated_engine.connect() as connection:
        quantity, kind = connection.execute(
            text("SELECT quantity, typeof(quantity) FROM line_items")
        ).one()

    assert (quantity, kind) == ("0.452", "text")
