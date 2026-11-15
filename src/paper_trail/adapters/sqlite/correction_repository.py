"""``CorrectionRepository`` on SQLite: ``corrections``."""

from dataclasses import replace

from sqlalchemy import Connection, insert, select, update

from paper_trail.adapters.sqlite.mappers import correction_from_row, correction_values
from paper_trail.adapters.sqlite.schema import corrections
from paper_trail.domain.enums import CorrectionKind
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import Correction, require_id


class SqliteCorrectionRepository:
    """Implements ``paper_trail.ports.repositories.CorrectionRepository``."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add(self, correction: Correction) -> Correction:
        correction_id = self._connection.execute(
            insert(corrections).values(correction_values(correction)).returning(corrections.c.id)
        ).scalar_one()
        return replace(correction, id=correction_id)

    def latest(
        self, kind: CorrectionKind, subject: str, store_key: str | None
    ) -> Correction | None:
        row = (
            self._connection.execute(
                select(corrections)
                .where(
                    corrections.c.kind == kind,
                    corrections.c.subject == subject,
                    corrections.c.store_key.is_not_distinct_from(store_key),
                )
                .order_by(corrections.c.created_at.desc(), corrections.c.id.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else correction_from_row(row)

    def list(self, kind: CorrectionKind | None = None) -> tuple[Correction, ...]:
        query = select(corrections).order_by(corrections.c.id)
        if kind is not None:
            query = query.where(corrections.c.kind == kind)
        return tuple(correction_from_row(row) for row in self._connection.execute(query).mappings())

    def update(self, correction: Correction) -> Correction:
        correction_id = require_id(correction.id)
        result = self._connection.execute(
            update(corrections)
            .where(corrections.c.id == correction_id)
            .values(correction_values(correction))
        )
        if not result.rowcount:
            msg = f"correction {correction_id} does not exist"
            raise NotFoundError(msg)
        return correction
