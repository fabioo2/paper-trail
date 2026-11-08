"""Keyset paging, newest first, with an opaque cursor (PLAN 3.2)."""

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import UTC, datetime

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
    if created_at.utcoffset() is None:
        msg = "cursor times must be timezone-aware"
        raise ValueError(msg)
    position = f"{created_at.astimezone(UTC).isoformat()}{_SEPARATOR}{id_}"
    return base64.urlsafe_b64encode(position.encode()).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    """The ``(created_at, id)`` a cursor points after; DomainValidationError if not ours."""
    try:
        position = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
        timestamp, separator, identifier = position.decode("utf-8").rpartition(_SEPARATOR)
        created_at = datetime.fromisoformat(timestamp)
    except (UnicodeError, binascii.Error, ValueError):
        raise _invalid_cursor() from None
    if not separator or created_at.utcoffset() is None or not _ID.fullmatch(identifier):
        raise _invalid_cursor()
    return created_at.astimezone(UTC), int(identifier)


def _invalid_cursor() -> DomainValidationError:
    return DomainValidationError("the cursor is invalid; start again from the first page")
