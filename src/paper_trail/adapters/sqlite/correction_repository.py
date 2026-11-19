"""``CorrectionRepository`` on SQLite: ``corrections``."""

from dataclasses import replace

from sqlalchemy import Connection, case, insert, select, update

from paper_trail.adapters.sqlite.mappers import correction_from_row, correction_values
from paper_trail.adapters.sqlite.schema import corrections
from paper_trail.domain.enums import CorrectionKind
from paper_trail.domain.models import Correction


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

    def rename_product(self, product_id: int, into_id: int) -> int:
        named, into = str(product_id), str(into_id)
        old, new = corrections.c.old_value, corrections.c.new_value
        result = self._connection.execute(
            update(corrections)
            .where(
                corrections.c.kind == CorrectionKind.PRODUCT_LINK, (old == named) | (new == named)
            )
            .values(
                old_value=case((old == named, into), else_=old),
                new_value=case((new == named, into), else_=new),
            )
        )
        return result.rowcount
