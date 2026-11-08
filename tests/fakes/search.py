"""In-memory search indexes (implement ``paper_trail.ports.search``) over a ``FakeState``.

The text index mimics the SQLite adapter's query semantics (every query token is a prefix term,
all tokens must match) without FTS5 ranking; the vector index is brute-force cosine distance.
"""

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence

from paper_trail.domain.errors import DomainValidationError
from paper_trail.ports.search import HitKind, TextHit, VectorHit
from tests.fakes.state import FakeState
from tests.support import require_id

EMBEDDING_DIMENSION = 768

_TOKEN = re.compile(r"[^\W_]+")


def _tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(char for char in folded if not unicodedata.combining(char))
    return _TOKEN.findall(without_marks)


def _score(query: Sequence[str], fields: Iterable[str | None]) -> int:
    document = [token for text in fields if text for token in _tokens(text)]
    matched = [term for term in query if any(token.startswith(term) for token in document)]
    return len(matched) if len(matched) == len(query) else 0


class FakeTextSearchIndex:
    def __init__(self, state: FakeState) -> None:
        self._state = state

    def search(self, query: str, *, limit: int) -> tuple[TextHit, ...]:
        terms = _tokens(query)
        if not terms:
            return ()
        hits = [
            TextHit(kind=HitKind.LINE_ITEM, id=require_id(item.id), score=float(score))
            for receipt in self._state.receipts.values()
            for item in receipt.items
            if (score := _score(terms, (item.description, item.raw_text)))
        ]
        hits += [
            TextHit(kind=HitKind.PRODUCT, id=require_id(product.id), score=float(score))
            for product in self._state.products.values()
            if (score := _score(terms, (product.canonical_name, product.brand, *product.tags)))
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.kind, hit.id))
        return tuple(hits[:limit])


class FakeVectorIndex:
    def __init__(self, state: FakeState, dimension: int = EMBEDDING_DIMENSION) -> None:
        self._state = state
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def upsert(self, product_id: int, vector: Sequence[float]) -> None:
        self._state.vectors[product_id] = self._checked(vector)

    def delete(self, product_id: int) -> None:
        self._state.vectors.pop(product_id, None)

    def nearest(self, vector: Sequence[float], *, k: int) -> tuple[VectorHit, ...]:
        query = self._checked(vector)
        hits = sorted(
            (
                VectorHit(product_id=product_id, distance=_cosine_distance(query, stored))
                for product_id, stored in self._state.vectors.items()
            ),
            key=lambda hit: (hit.distance, hit.product_id),
        )
        return tuple(hits[:k])

    def _checked(self, vector: Sequence[float]) -> tuple[float, ...]:
        if len(vector) != self._dimension:
            msg = f"expected a vector of length {self._dimension}, got {len(vector)}"
            raise DomainValidationError(msg)
        return tuple(float(value) for value in vector)


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    norms = math.hypot(*a) * math.hypot(*b)
    if norms == 0:
        return 1.0
    return 1.0 - math.fsum(x * y for x, y in zip(a, b, strict=True)) / norms
