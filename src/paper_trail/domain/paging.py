"""Keyset paging, newest first, with an opaque cursor (PLAN 3.2)."""

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from paper_trail.domain.errors import DomainValidationError

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100

_SEPARATOR = "|"
_ID = re.compile(r"[0-9]+")


@dataclass(frozen=True, slots=True)
class PageRequest:
    limit: int = DEFAULT_PAGE_LIMIT
    cursor: str | None = None  # from a previous Page.next_cursor

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_PAGE_LIMIT:
            msg = f"limit must be between 1 and {MAX_PAGE_LIMIT}, got {self.limit}"
            raise DomainValidationError(msg)


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    next_cursor: str | None  # None on the last page


def encode_cursor(created_at: datetime, id_: int) -> str:
    """The cursor after the row ``(created_at, id_)``: URL-safe base64 of "<iso time>|<id>"."""
    return _encode(_position(created_at, id_))


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    """The ``(created_at, id)`` a cursor points after; DomainValidationError if not ours."""
    return _parse_position(_decode(cursor))


def encode_dated_cursor(purchased_on: date | None, created_at: datetime, id_: int) -> str:
    """The cursor after the row ``(purchased_on, created_at, id_)`` of a list sorted by
    purchase date first: URL-safe base64 of "<date, or nothing>|<iso time>|<id>"."""
    day = "" if purchased_on is None else purchased_on.isoformat()
    return _encode(f"{day}{_SEPARATOR}{_position(created_at, id_)}")


def decode_dated_cursor(cursor: str) -> tuple[date | None, datetime, int]:
    """The ``(purchased_on, created_at, id)`` a dated cursor points after; DomainValidationError
    if not ours."""
    day, separator, position = _decode(cursor).partition(_SEPARATOR)
    try:
        purchased_on = date.fromisoformat(day) if day else None
    except ValueError:
        raise _invalid_cursor() from None
    if not separator:
        raise _invalid_cursor()
    return (purchased_on, *_parse_position(position))


def _position(created_at: datetime, id_: int) -> str:
    if created_at.utcoffset() is None:
        msg = "cursor times must be timezone-aware"
        raise ValueError(msg)
    return f"{created_at.astimezone(UTC).isoformat()}{_SEPARATOR}{id_}"


def _encode(position: str) -> str:
    return base64.urlsafe_b64encode(position.encode()).decode("ascii")


def _decode(cursor: str) -> str:
    try:
        return base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True).decode()
    except (UnicodeError, binascii.Error, ValueError):
        raise _invalid_cursor() from None


def _parse_position(position: str) -> tuple[datetime, int]:
    timestamp, separator, identifier = position.rpartition(_SEPARATOR)
    try:
        created_at = datetime.fromisoformat(timestamp)
    except ValueError:
        raise _invalid_cursor() from None
    if not separator or created_at.utcoffset() is None or not _ID.fullmatch(identifier):
        raise _invalid_cursor()
    return created_at.astimezone(UTC), int(identifier)


def _invalid_cursor() -> DomainValidationError:
    return DomainValidationError("the cursor is invalid; start again from the first page")
