"""``EmbeddingPort`` on Ollama's native embed API (PLAN 6, ADR-0009 amendment, S4-AC-08).

One ``POST <base_url>/api/embed`` per batch, ``{"model", "input", "keep_alive"}``. An answer
whose vectors do not fit the texts (count) or the vector index (length), or whose elements are
not all JSON numbers, is an ``LlmOutputError``: the configured model is not the one the index
was built for, or it answered something other than vectors.
"""

from collections.abc import Sequence

from paper_trail.adapters.ollama.http import ollama_client, post_json
from paper_trail.config import OllamaSettings
from paper_trail.ports.embeddings import EMBEDDING_DIMENSION, Vector
from paper_trail.ports.llm import LlmOutputError


class OllamaEmbeddings:
    """Implements ``paper_trail.ports.embeddings.EmbeddingPort``."""

    def __init__(self, settings: OllamaSettings) -> None:
        self._settings = settings

    def embed(self, texts: Sequence[str]) -> tuple[Vector, ...]:
        if not texts:
            return ()
        body = {
            "model": self._settings.embed_model,
            "input": list(texts),
            "keep_alive": self._settings.keep_alive,
        }
        with ollama_client(self._settings) as client:
            answer = post_json(client, "api/embed", body)
        return _vectors(answer.get("embeddings"), len(texts))


def _vectors(embeddings: object, count: int) -> tuple[Vector, ...]:
    if not isinstance(embeddings, list) or len(embeddings) != count:
        msg = f"the embedding model did not answer one vector for each of {count} texts"
        raise LlmOutputError(msg)
    vectors: list[Vector] = []
    for vector in embeddings:
        if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSION:
            msg = f"the embedding model did not answer {EMBEDDING_DIMENSION}-long vectors"
            raise LlmOutputError(msg)
        if not all(_is_number(value) for value in vector):
            msg = "the embedding model answered a vector element that is not a number"
            raise LlmOutputError(msg)
        vectors.append(tuple(float(value) for value in vector))
    return tuple(vectors)


def _is_number(value: object) -> bool:
    """A JSON number; ``bool`` is excluded, though Python counts it as an ``int``."""
    return isinstance(value, int | float) and not isinstance(value, bool)
