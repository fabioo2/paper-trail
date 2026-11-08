"""Column types that keep times, dates, decimals, enums and JSON exact in SQLite TEXT columns.

SQLite has no such types of its own; each value is stored as text that round-trips exactly
(PLAN 1.2, 3.11). Binding a value of the wrong kind fails the statement instead of storing an
approximation.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Dialect, Text
from sqlalchemy.types import TypeDecorator


class UtcDateTime(TypeDecorator[datetime]):
    """An aware datetime as fixed-width ISO-8601 UTC text, e.g.
    ``2026-01-01T12:00:00.000000+00:00``, so that text order is time order."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            msg = "naive datetimes are not stored; use a timezone-aware UTC datetime"
            raise ValueError(msg)
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        return None if value is None else datetime.fromisoformat(value)


class IsoDate(TypeDecorator[date]):
    """A calendar date as ISO-8601 text, e.g. ``2026-11-01``."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: date | None, dialect: Dialect) -> str | None:
        return None if value is None else value.isoformat()

    def process_result_value(self, value: str | None, dialect: Dialect) -> date | None:
        return None if value is None else date.fromisoformat(value)


class DecimalText(TypeDecorator[Decimal]):
    """A Decimal as its exact text, e.g. ``0.452``; floats are refused."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal) or not value.is_finite():
            msg = f"expected a finite Decimal, got {value!r}"
            raise TypeError(msg)
        return str(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


class StrEnumType[E: StrEnum](TypeDecorator[E]):
    """A StrEnum member as its value; loads the member back."""

    impl = Text
    cache_ok = True

    def __init__(self, enum_class: type[E]) -> None:
        super().__init__()
        self.enum_class = enum_class

    def process_bind_param(self, value: E | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, self.enum_class):
            msg = f"expected a {self.enum_class.__name__}, got {value!r}"
            raise TypeError(msg)
        return value.value

    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        return None if value is None else self.enum_class(value)


class JsonText(TypeDecorator[Any]):
    """A JSON document as text (UTF-8, not ASCII-escaped, so FTS5 can index its words)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:  # noqa: ANN401 - any JSON value
        return None if value is None else json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Any:  # noqa: ANN401 - any JSON value
        return None if value is None else json.loads(value)
