"""Text embeddings for product matching (PLAN 6, ADR-0009 amendment)."""

from collections.abc import Sequence
from typing import Protocol

EMBEDDING_DIMENSION = 768  # nomic-embed-text (measured 2026-11-06); the vector index's width

type Vector = tuple[float, ...]


class EmbeddingPort(Protocol):
    def embed(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        """One ``EMBEDDING_DIMENSION``-long vector per text, in order; ``()`` for no texts
        (and no request). Raises ``LlmUnavailableError`` (``ports.llm``) when the host cannot be
        reached, ``LlmOutputError`` when its answer has the wrong shape."""
        ...
