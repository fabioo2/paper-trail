"""Looking up or guessing the products a receipt created (claude-extraction plan D3, V8): what
the receipt graph hands the enricher and what it gets back (``application.enrichment``)."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from paper_trail.domain.enums import FieldSource
from paper_trail.ports.lookups import LookupSource


class EnrichOutcome(StrEnum):
    LOOKED_UP = "looked up"  # a catalog or a database filled at least one field
    GUESSED = "guessed"  # only the guess filled fields
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"  # the guess was needed but the monthly Claude budget is reached
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EnrichTarget:
    """A product to enrich and what it was printed as: ``code`` is the code as read (any
    scheme), ``gtin`` its barcode when it has one (what the databases are asked)."""

    product_id: int
    printed_text: str
    store_name: str
    store_key: str
    department: str | None = None
    code: str | None = None
    gtin: str | None = None


@dataclass(frozen=True, slots=True)
class LookupEvent:
    """One database's answer for a code: ``hit``, ``not_found``, ``cached``, ``error`` or
    ``rate_limited``."""

    source: LookupSource
    code: str
    result: str


@dataclass(frozen=True, slots=True)
class ProductEnriched:
    """What enriching one product did: the fields written with their sources, the databases
    asked, whether Claude was asked, and why it failed."""

    product_id: int
    outcome: EnrichOutcome
    written: Mapping[str, FieldSource] = field(default_factory=dict)
    lookups: tuple[LookupEvent, ...] = ()
    guessed: bool = False
    error: str | None = None


class Enricher(Protocol):
    def enrich_all(self, targets: Iterable[EnrichTarget]) -> list[ProductEnriched]:
        """Look up or guess each target, each written in its own unit of work; a product that
        fails is reported, never raised."""
        ...
