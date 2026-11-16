"""Full-text search over line items and products."""

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


class TextSearchIndex(Protocol):
    def search(self, query: str, *, limit: int) -> tuple[TextHit, ...]:
        """Line items and products matching every word of ``query`` as a prefix, best first.

        The query is plain text: search syntax in it is never interpreted and never raises.
        """
        ...
