"""PriceRepository contract (S1-AC-10), for the fake and the SQLite adapter.

Pinned (docs/tests/stage1.md): a second observation for the same line item raises ConflictError.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import PriceObservation
from paper_trail.domain.money import Money
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import price_observation, product
from tests.storage import in_unit, seed_product_and_receipt
from tests.support import require_id


def _observations(
    uow_factory: UnitOfWorkFactory, rows: list[PriceObservation]
) -> tuple[PriceObservation, ...]:
    return in_unit(uow_factory, lambda uow: uow.prices.add_many(rows))


def _history(uow_factory: UnitOfWorkFactory, product_id: int) -> tuple[PriceObservation, ...]:
    with uow_factory() as uow:
        return uow.prices.history(product_id)


def test_s1_ac_10_add_many_assigns_ids_and_round_trips(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: add_many assigns ids; history returns them unchanged (Decimal, currency)."""
    product_id, receipt_id, (eggs, milk) = seed_product_and_receipt(uow_factory)
    rows = [
        price_observation(product_id=product_id, receipt_id=receipt_id, line_item_id=eggs),
        price_observation(
            product_id=product_id,
            receipt_id=receipt_id,
            line_item_id=milk,
            quantity=Decimal("0.452"),
            unit_price=Money(1099, "USD"),
            line_total=Money(497, "USD"),
        ),
    ]

    stored = _observations(uow_factory, rows)

    assert all(row.id is not None for row in stored)
    assert [replace(row, id=None) for row in stored] == rows
    assert _history(uow_factory, product_id) == stored
    assert str(stored[1].quantity) == "0.452"


def test_s1_ac_10_add_many_of_nothing(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: an empty batch is a no-op."""
    assert _observations(uow_factory, []) == ()


def test_s1_ac_10_history_is_by_date_then_id(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: history is ordered by observed_on asc, then id asc; other products excluded."""
    product_id, _, _ = seed_product_and_receipt(uow_factory)
    _, receipt_id, line_ids = seed_product_and_receipt(uow_factory)
    _, other_receipt, other_lines = seed_product_and_receipt(uow_factory)
    other_product = in_unit(uow_factory, lambda uow: uow.products.add(product(canonical_name="X")))
    later, earlier = line_ids
    stored = _observations(
        uow_factory,
        [
            price_observation(product_id=product_id, receipt_id=receipt_id, line_item_id=later,
                              observed_on=date(2026, 11, 2)),
            price_observation(product_id=product_id, receipt_id=receipt_id, line_item_id=earlier,
                              observed_on=date(2026, 10, 30)),
            price_observation(product_id=product_id, receipt_id=other_receipt,
                              line_item_id=other_lines[0], observed_on=date(2026, 11, 2)),
            price_observation(product_id=require_id(other_product.id), receipt_id=other_receipt,
                              line_item_id=other_lines[1]),
        ],
    )  # fmt: skip

    history = _history(uow_factory, product_id)

    assert history == (stored[1], stored[0], stored[2])


def test_s1_ac_10_history_of_unknown_product_is_empty(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: no observations, no history."""
    assert _history(uow_factory, 4242) == ()


def test_s1_ac_10_one_observation_per_line_item(uow_factory: UnitOfWorkFactory) -> None:
    """Pinned: line_item_id is unique; a second observation for a line raises ConflictError."""
    product_id, receipt_id, (eggs, _) = seed_product_and_receipt(uow_factory)
    row = price_observation(product_id=product_id, receipt_id=receipt_id, line_item_id=eggs)
    _observations(uow_factory, [row])

    _, other_receipt, (other_line, _) = seed_product_and_receipt(uow_factory)
    twice = replace(row, receipt_id=other_receipt, line_item_id=other_line)

    with pytest.raises(ConflictError):
        _observations(uow_factory, [row])
    with pytest.raises(ConflictError):
        _observations(uow_factory, [twice, twice])
    assert len(_history(uow_factory, product_id)) == 1
