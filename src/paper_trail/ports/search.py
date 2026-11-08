"""Full-text search over line items and products, and nearest-neighbour product vectors."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Protocol


class HitKind(StrEnum):
    LINE_ITEM = auto()
    PRODUCT = auto()


@dataclass(frozen=True, slots=True)
class TextHit:
    kind: HitKind
    id: int
    score: float  # higher is better


@dataclass(frozen=True, slots=True)
class VectorHit:
    product_id: int
    distance: float  # cosine distance: 0 is the same direction, 2 the opposite


class TextSearchIndex(Protocol):
    def search(self, query: str, *, limit: int) -> tuple[TextHit, ...]:
        """Line items and products matching every word of ``query`` as a prefix, best first.

        The query is plain text: search syntax in it is never interpreted and never raises.
        """
        ...


class VectorIndex(Protocol):
    @property
    def dimension(self) -> int: ...

    def upsert(self, product_id: int, vector: Sequence[float]) -> None:
        """Store or replace the product's vector; DomainValidationError on a wrong length."""
        ...

    def delete(self, product_id: int) -> None:
        """Forget the product's vector (no-op when it has none)."""
        ...

    def nearest(self, vector: Sequence[float], *, k: int) -> tuple[VectorHit, ...]:
        """At most ``k`` products by ascending distance (ties by product id)."""
        ...
