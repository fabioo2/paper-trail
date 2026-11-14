"""PriceRepository contract (S1-AC-10), for the fake and the SQLite adapter.

Pinned (docs/tests/stage1.md): a second observation for the same line item raises ConflictError.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text

from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import PriceObservation
from paper_trail.domain.money import Money
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import cad, price_observation, product
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, seed_product_and_receipt, sqlite_uow_factory
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


# --- discounts and paid prices (discounts-and-paid-prices plan R5, R8, R9, A5) ------------------

# P12 SET_DISCOUNT_QUANTITIES: (quantity, unit price, line total, discount) -> (unit discount, paid)
type DiscountRow = tuple[Decimal, int, int, int, int, int]

SET_DISCOUNT_QUANTITIES = [
    pytest.param((Decimal("1"), 1699, 1699, -400, -400, 1299), id="one item"),
    pytest.param((Decimal("2"), 329, 658, -5, -3, 653), id="two items, -2.5 rounds away from 0"),
    pytest.param((Decimal("0.460"), 1500, 690, -100, -217, 590), id="weighed 0.460 kg"),
]


def test_tc_81_discount_and_paid_price_round_trip(uow_factory: UnitOfWorkFactory) -> None:
    product_id, receipt_id, (eggs, milk) = seed_product_and_receipt(uow_factory)
    rows = [
        price_observation(
            product_id=product_id, receipt_id=receipt_id, line_item_id=eggs, discount=cad(-500)
        ),
        price_observation(product_id=product_id, receipt_id=receipt_id, line_item_id=milk),
    ]

    stored = _observations(uow_factory, rows)
    history = _history(uow_factory, product_id)

    assert ([replace(row, id=None) for row in stored], history) == (rows, stored)
    assert [(row.discount, row.unit_discount, row.paid, row.on_sale) for row in history] == [
        (cad(-500), cad(-500), cad(399), True),
        (cad(0), cad(0), cad(899), False),
    ]


def _observe(
    uow_factory: UnitOfWorkFactory, quantity: Decimal, unit_price: int, line_total: int
) -> tuple[PriceObservation, int]:
    """One regular observation on the default receipt's first line; returns it and the id of
    the receipt's second line, which has none."""
    product_id, receipt_id, (first, second) = seed_product_and_receipt(uow_factory)
    (stored,) = _observations(
        uow_factory,
        [
            price_observation(
                product_id=product_id,
                receipt_id=receipt_id,
                line_item_id=first,
                quantity=quantity,
                unit_price=cad(unit_price),
                line_total=cad(line_total),
            )
        ],
    )
    return stored, second


def test_tc_82_set_discount_updates_the_lines_observation(uow_factory: UnitOfWorkFactory) -> None:
    """Only the discount changes (product, quantity and regular prices stay); the stored
    columns for other quantities are pinned on SQLite below."""
    stored, _ = _observe(uow_factory, Decimal("1"), 1699, 1699)

    updated = in_unit(
        uow_factory, lambda uow: uow.prices.set_discount(stored.line_item_id, cad(-400))
    )

    expected = replace(stored, discount=cad(-400))
    assert (updated, _history(uow_factory, stored.product_id)) == (expected, (expected,))


def test_tc_82_set_discount_on_a_line_without_an_observation_is_none(
    uow_factory: UnitOfWorkFactory,
) -> None:
    stored, unobserved = _observe(uow_factory, Decimal("1"), 899, 899)

    updated = in_unit(uow_factory, lambda uow: uow.prices.set_discount(unobserved, cad(-100)))

    assert (updated, _history(uow_factory, stored.product_id)) == (None, (stored,))


def test_tc_83_reassign_keeps_the_discount(uow_factory: UnitOfWorkFactory) -> None:
    product_id, receipt_id, (eggs, _) = seed_product_and_receipt(uow_factory)
    (stored,) = _observations(
        uow_factory,
        [
            price_observation(
                product_id=product_id, receipt_id=receipt_id, line_item_id=eggs, discount=cad(-500)
            )
        ],
    )
    other = require_id(in_unit(uow_factory, lambda uow: uow.products.add(product())).id)

    moved = in_unit(uow_factory, lambda uow: uow.prices.reassign(eggs, other))

    expected = replace(stored, product_id=other)
    (history,) = _history(uow_factory, other)
    assert (moved, history, history.discount) == (expected, expected, cad(-500))


def _stored_discount_columns(engine: Engine, line_item_id: int) -> tuple[int, ...]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT discount_cents, unit_discount_cents, paid_cents, on_sale"
                " FROM price_observations WHERE line_item_id = :id"
            ),
            {"id": line_item_id},
        ).one()
    return tuple(row)


@pytest.mark.parametrize("row", SET_DISCOUNT_QUANTITIES)
def test_tc_81_82_sqlite_stores_the_domain_values(
    migrated_engine: Engine, row: DiscountRow
) -> None:
    """The derived columns ``add_many`` and ``set_discount`` write equal the domain's values
    (the DB has no CHECK tying the unit discount to the quantity)."""
    quantity, unit_price, line_total, discount, unit_discount, paid = row
    uow_factory = sqlite_uow_factory(migrated_engine, FakeClock())
    stored, second = _observe(uow_factory, quantity, unit_price, line_total)
    regular = _stored_discount_columns(migrated_engine, stored.line_item_id)
    _observations(
        uow_factory, [replace(stored, id=None, line_item_id=second, discount=cad(discount))]
    )
    added = _stored_discount_columns(migrated_engine, second)

    in_unit(uow_factory, lambda uow: uow.prices.set_discount(stored.line_item_id, cad(discount)))

    discounted = (discount, unit_discount, paid, 1)
    assert regular == (0, 0, line_total, 0)
    assert added == discounted
    assert _stored_discount_columns(migrated_engine, stored.line_item_id) == discounted
