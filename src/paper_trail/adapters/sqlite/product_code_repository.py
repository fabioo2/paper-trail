"""``ProductCodeRepository`` on SQLite: ``product_codes``."""

import dataclasses

import structlog
from sqlalchemy import Connection, delete, insert, select, update

from paper_trail.adapters.sqlite.mappers import product_code_from_row, product_code_values
from paper_trail.adapters.sqlite.schema import product_codes
from paper_trail.domain.enums import CodeScheme
from paper_trail.domain.models import ProductCode, require_id

log = structlog.get_logger(__name__)


class SqliteProductCodeRepository:
    """Implements ``paper_trail.ports.repositories.ProductCodeRepository``."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def find(self, scheme: CodeScheme, store_key: str, code: str) -> ProductCode | None:
        row = (
            self._connection.execute(
                select(product_codes).where(
                    product_codes.c.scheme == scheme,
                    product_codes.c.store_key == store_key,
                    product_codes.c.code == code,
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else product_code_from_row(row)

    def for_product(self, product_id: int) -> tuple[ProductCode, ...]:
        rows = self._connection.execute(
            select(product_codes)
            .where(product_codes.c.product_id == product_id)
            .order_by(product_codes.c.id)
        ).mappings()
        return tuple(product_code_from_row(row) for row in rows)

    def list(
        self, *, store_key: str | None = None, scheme: CodeScheme | None = None
    ) -> tuple[ProductCode, ...]:
        query = select(product_codes).order_by(product_codes.c.id)
        if store_key is not None:
            query = query.where(product_codes.c.store_key == store_key)
        if scheme is not None:
            query = query.where(product_codes.c.scheme == scheme)
        return tuple(
            product_code_from_row(row) for row in self._connection.execute(query).mappings()
        )

    def upsert(self, code: ProductCode, *, replace: bool) -> ProductCode:
        existing = self.find(code.scheme, code.store_key, code.code)
        if existing is None:
            code_id = self._connection.execute(
                insert(product_codes)
                .values(product_code_values(code))
                .returning(product_codes.c.id)
            ).scalar_one()
            return dataclasses.replace(code, id=code_id)
        same_product = existing.product_id == code.product_id
        if not replace:
            if not same_product:
                log.warning(
                    "catalog.code_conflict",
                    scheme=code.scheme.value,
                    store_key=code.store_key,
                    code=code.code,
                    existing_product_id=existing.product_id,
                    refused_product_id=code.product_id,
                )
            return existing
        if same_product and existing.source is code.source:
            return existing
        stored = dataclasses.replace(
            existing, product_id=code.product_id, source=code.source, updated_at=code.updated_at
        )
        self._connection.execute(
            update(product_codes)
            .where(product_codes.c.id == require_id(existing.id))
            .values(
                product_id=stored.product_id, source=stored.source, updated_at=stored.updated_at
            )
        )
        return stored

    def remove(self, scheme: CodeScheme, store_key: str, code: str) -> bool:
        result = self._connection.execute(
            delete(product_codes).where(
                product_codes.c.scheme == scheme,
                product_codes.c.store_key == store_key,
                product_codes.c.code == code,
            )
        )
        return bool(result.rowcount)
