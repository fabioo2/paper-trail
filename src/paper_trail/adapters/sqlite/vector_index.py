"""``VectorIndex`` over the sqlite-vec table ``product_vectors`` (rowid = product id).

Distances are computed with ``vec_distance_cosine`` and ordered by (distance, product id), so
equal distances come back in a stable order; vec0's own KNN scan is a full scan as well and
does not break ties.
"""

import re
from collections.abc import Sequence

import sqlite_vec
from sqlalchemy import Connection, text

from paper_trail.domain.errors import DomainValidationError
from paper_trail.ports.embeddings import EMBEDDING_DIMENSION  # the migration creates float[768]
from paper_trail.ports.search import VectorHit

__all__ = ["EMBEDDING_DIMENSION", "SqliteVectorIndex", "declared_dimension"]

_DECLARED_DIMENSION = re.compile(r"float\[([0-9]+)\]")
_DECLARATION = text(
    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'product_vectors'"
)
_DELETE = text("DELETE FROM product_vectors WHERE rowid = :product_id")
_INSERT = text("INSERT INTO product_vectors (rowid, embedding) VALUES (:product_id, :embedding)")
_NEAREST = text(
    """
    SELECT rowid AS product_id, vec_distance_cosine(embedding, :embedding) AS distance
    FROM product_vectors
    ORDER BY distance, rowid
    LIMIT :k
    """
)


def declared_dimension(connection: Connection) -> int | None:
    """The vector length ``product_vectors`` was created with (None if there is no such table)."""
    declaration: str | None = connection.execute(_DECLARATION).scalar_one_or_none()
    match = _DECLARED_DIMENSION.search(declaration or "")
    return None if match is None else int(match.group(1))


class SqliteVectorIndex:
    """Implements ``paper_trail.ports.search.VectorIndex``."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIMENSION

    def upsert(self, product_id: int, vector: Sequence[float]) -> None:
        embedding = self._serialized(vector)
        # vec0 tables support neither UPSERT nor INSERT OR REPLACE.
        self._connection.execute(_DELETE, {"product_id": product_id})
        self._connection.execute(_INSERT, {"product_id": product_id, "embedding": embedding})

    def delete(self, product_id: int) -> None:
        self._connection.execute(_DELETE, {"product_id": product_id})

    def nearest(self, vector: Sequence[float], *, k: int) -> tuple[VectorHit, ...]:
        rows = self._connection.execute(_NEAREST, {"embedding": self._serialized(vector), "k": k})
        return tuple(
            VectorHit(product_id=product_id, distance=float(distance))
            for product_id, distance in rows
        )

    def _serialized(self, vector: Sequence[float]) -> bytes:
        if len(vector) != EMBEDDING_DIMENSION:
            msg = f"expected a vector of length {EMBEDDING_DIMENSION}, got {len(vector)}"
            raise DomainValidationError(msg)
        serialized: bytes = sqlite_vec.serialize_float32(list(vector))
        return serialized
