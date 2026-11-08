"""``PriceRepository`` on SQLite: ``price_observations``."""

from collections.abc import Sequence
from dataclasses import replace

from sqlalchemy import Connection, insert, select, update

from paper_trail.adapters.sqlite.integrity import unique_violation_as_conflict
from paper_trail.adapters.sqlite.mappers import price_from_row, price_values
from paper_trail.adapters.sqlite.schema import price_observations
from paper_trail.domain.models import PriceObservation


class SqlitePriceRepository:
    """Implements ``paper_trail.ports.repositories.PriceRepository``."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add_many(self, observations: Sequence[PriceObservation]) -> tuple[PriceObservation, ...]:
        if not observations:
            return ()
        with unique_violation_as_conflict("a line item has at most one price observation"):
            ids = self._connection.execute(
                insert(price_observations).returning(
                    price_observations.c.id, sort_by_parameter_order=True
                ),
                [price_values(observation) for observation in observations],
            ).scalars()
            return tuple(
                replace(observation, id=observation_id)
                for observation, observation_id in zip(observations, ids, strict=True)
            )

    def history(self, product_id: int) -> tuple[PriceObservation, ...]:
        rows = self._connection.execute(
            select(price_observations)
            .where(price_observations.c.product_id == product_id)
            .order_by(price_observations.c.observed_on, price_observations.c.id)
        ).mappings()
        return tuple(price_from_row(row) for row in rows)

    def reassign(self, line_item_id: int, product_id: int) -> PriceObservation | None:
        row = (
            self._connection.execute(
                update(price_observations)
                .where(price_observations.c.line_item_id == line_item_id)
                .values(product_id=product_id)
                .returning(*price_observations.c)
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else price_from_row(row)
