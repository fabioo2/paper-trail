"""Online product databases asked for a barcode (claude-extraction plan D3, V10).

A lookup sends the barcode and nothing else. Its answer is a hit, ``None`` (the database does
not know the code) or an error: an error is never remembered, so the code is asked again on the
next run; a rate limit (``LookupRateLimitedError``) also stops asking that database for the rest
of the run. Answers are remembered in ``product_lookups`` (``LookupCache``): a hit for good, a
miss for 30 days.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

type LookupSource = Literal["off", "obf", "opf", "upcitemdb"]


@dataclass(frozen=True, slots=True)
class LookupHit:
    """What a database says a code is, as it says it (no field is required but the name)."""

    name: str
    brand: str | None = None
    quantity: str | None = None  # the size as the database prints it, e.g. "500 g"
    categories: tuple[str, ...] = ()  # the database's own category labels


class LookupFailedError(Exception):
    """The database could not answer now (an error status, a timeout, no connection, a body
    that is not its JSON). Nothing is remembered."""


class LookupRateLimitedError(LookupFailedError):
    """The database asked us to slow down (429), or today's quota is used up."""


class ProductLookup(Protocol):
    @property
    def source(self) -> LookupSource: ...

    def lookup(self, code: str) -> LookupHit | None:
        """The database's entry for the GTIN ``code`` (digits only), or None when it has none.
        Raises ``LookupFailedError`` (or ``LookupRateLimitedError``) for every failure."""
        ...


@dataclass(frozen=True, slots=True)
class CachedLookup:
    """A remembered answer: ``hit`` None means the database did not know the code."""

    code: str
    source: LookupSource
    hit: LookupHit | None
    fetched_at: datetime


class LookupCache(Protocol):
    def get(self, code: str, source: LookupSource) -> CachedLookup | None:
        """The remembered answer, or None when the database was never asked (or only failed)."""
        ...

    def put(self, answer: CachedLookup) -> None:
        """Remember ``answer``, replacing an earlier one for its code and source."""
        ...

    def reserve_request(
        self, source: LookupSource, at: datetime, *, since: datetime, limit: int
    ) -> bool:
        """Record a request to ``source`` at ``at`` unless ``limit`` requests were recorded at
        or after ``since``; True when it was recorded (the request may be sent). Every request
        counts, whatever its answer. Called in a writing unit, so two processes never both
        take the last slot."""
        ...
