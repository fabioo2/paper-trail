"""Translating SQLite constraint violations into domain errors."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.exc import IntegrityError

from paper_trail.domain.errors import ConflictError

_UNIQUE_VIOLATION = "UNIQUE constraint failed"


@contextmanager
def unique_violation_as_conflict(message: str) -> Iterator[None]:
    """Raise ``ConflictError(message)`` for a UNIQUE violation inside the block.

    SQLite undoes only the failed statement; the transaction stays usable. Other integrity
    errors (a foreign key to nothing, a CHECK) are programming errors and propagate.
    """
    try:
        yield
    except IntegrityError as error:
        if _UNIQUE_VIOLATION not in str(error.orig):
            raise
        raise ConflictError(message) from error
