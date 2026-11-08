"""Deterministic embeddings (implement ``paper_trail.ports.embeddings.EmbeddingPort``).

A text found in the table gets its fixed vector; any other text gets a unit vector seeded by the
text's hash. Two hashed vectors in 768 dimensions are almost orthogonal (cosine distance about
1 +- 0.04), far above ``MATCHING__REVIEW_MAX_DISTANCE``, so an unknown line is never "similar"
by accident. ``basis`` and ``at_distance`` build table vectors at an exact cosine distance from
each other, to put a line below, inside or above the matching thresholds (S4-AC-02..04).

``error`` (e.g. ``LlmUnavailableError``) is raised by every call instead, for a GPU that dropped;
the texts are still recorded. ``calls`` counts requests, as the adapter counts host requests.
Every graph and tool test drives embeddings through this fake, never a real model.
"""

import hashlib
import math
import random
from collections.abc import Mapping, Sequence

EMBEDDING_DIMENSION = 768  # nomic-embed-text, measured 2026-11-06

type Vector = tuple[float, ...]


def basis(axis: int) -> Vector:
    """The unit vector along ``axis``."""
    values = [0.0] * EMBEDDING_DIMENSION
    values[axis] = 1.0
    return tuple(values)


def at_distance(distance: float, *, axis: int = 0, toward: int = 1) -> Vector:
    """A unit vector whose cosine distance from ``basis(axis)`` is ``distance`` (0..1), turned
    toward ``basis(toward)``. Vectors turned toward different axes are spread apart."""
    if not 0.0 <= distance <= 1.0 or axis == toward:
        msg = f"cannot place a vector at distance {distance} from axis {axis} toward {toward}"
        raise ValueError(msg)
    cosine = 1.0 - distance
    values = [0.0] * EMBEDDING_DIMENSION
    values[axis] = cosine
    values[toward] = math.sqrt(1.0 - cosine * cosine)
    return tuple(values)


def hashed_vector(text: str) -> Vector:
    """A deterministic pseudo-random unit vector for ``text``."""
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    generator = random.Random(seed)  # noqa: S311 - test data, not cryptography
    values = [generator.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIMENSION)]
    norm = math.hypot(*values)
    return tuple(value / norm for value in values)


class FakeEmbeddings:
    def __init__(
        self, table: Mapping[str, Sequence[float]] | None = None, *, error: Exception | None = None
    ) -> None:
        self._table = {text: tuple(vector) for text, vector in (table or {}).items()}
        self._error = error
        self.requests: list[tuple[str, ...]] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def texts(self) -> list[str]:
        """Every text embedded so far, in order."""
        return [text for request in self.requests for text in request]

    def embed(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        """One vector per text, in order. An empty batch is no call (like the adapter)."""
        if not texts:
            return ()
        self.requests.append(tuple(texts))
        if self._error is not None:
            raise self._error
        return tuple(self.vector(text) for text in texts)

    def vector(self, text: str) -> Vector:
        """What ``embed`` returns for ``text`` (without recording a call)."""
        return self._table.get(text) or hashed_vector(text)
