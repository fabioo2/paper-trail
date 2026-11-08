"""Readiness probe for the SQLite database."""

from pathlib import Path

import structlog
from sqlalchemy import Engine, text

from paper_trail.adapters.sqlite.migrations import head_revision, revision_of
from paper_trail.adapters.sqlite.vector_index import EMBEDDING_DIMENSION, declared_dimension
from paper_trail.domain.health import ComponentHealth, HealthStatus

log = structlog.get_logger(__name__)

_IN_MEMORY = ":memory:"


class DatabaseHealthProbe:
    """Implements ``paper_trail.ports.health.HealthProbe`` for the application database.

    Ready means: the file exists and opens, the schema is at the Alembic head, sqlite-vec is
    loaded, SQLite was built with FTS5 and the product vectors have the embedding model's
    dimension. Probing has no side effects: a missing database is
    reported, never created (opening it through the engine would create the file).
    """

    name = "database"

    def __init__(self, engine: Engine) -> None:
        database = engine.url.database
        if not database or database == _IN_MEMORY:
            msg = "DatabaseHealthProbe needs an engine bound to a database file"
            raise ValueError(msg)
        self._engine = engine
        self._database_path = Path(database)

    def check(self) -> ComponentHealth:
        # A probe must never raise: every failure below means "not ready".
        try:
            head = head_revision()
        except Exception:
            log.warning("health.migrations_unavailable", exc_info=True)
            return self._error("the migration scripts cannot be read", {})
        if not self._database_path.exists():
            return self._error(
                "the database does not exist; run `paper-trail db upgrade`", {"head": head}
            )
        try:
            with self._engine.connect() as connection:
                revision = revision_of(connection)
                sqlite_version, vec_version, has_fts5 = connection.execute(
                    text(
                        "SELECT sqlite_version(), vec_version(),"
                        " sqlite_compileoption_used('ENABLE_FTS5')"
                    )
                ).one()
                vector_dimension = declared_dimension(connection)
        except Exception:
            log.warning("health.database_unavailable", exc_info=True)
            return self._error("the database cannot be opened", {"head": head})

        data = {
            "revision": revision or "none",
            "head": head,
            "sqlite_version": str(sqlite_version),
            "sqlite_vec": str(vec_version),
            "vector_dimension": str(vector_dimension or "none"),
        }
        problem = _schema_problem(
            revision, head, has_fts5=bool(has_fts5), dimension=vector_dimension
        )
        if problem is not None:
            return self._error(problem, data)
        return ComponentHealth(name=self.name, status=HealthStatus.OK, data=data)

    def _error(self, detail: str, data: dict[str, str]) -> ComponentHealth:
        return ComponentHealth(name=self.name, status=HealthStatus.ERROR, detail=detail, data=data)


def _schema_problem(
    revision: str | None, head: str, *, has_fts5: bool, dimension: int | None
) -> str | None:
    """Why an open database is not ready, or None when it is."""
    if revision != head:
        return (
            f"schema is at revision {revision or 'none'}, expected {head}; "
            "run `paper-trail db upgrade`"
        )
    if not has_fts5:
        return "SQLite was built without FTS5"
    if dimension != EMBEDDING_DIMENSION:
        return (
            f"product_vectors holds vectors of dimension {dimension or 'none'},"
            f" expected {EMBEDDING_DIMENSION}"
        )
    return None
