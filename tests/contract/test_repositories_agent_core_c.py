"""Storage port additions of agent core slice C, for the fake and SQLite (agent-core plan 8
delta 5, PLAN 6).

Pinned (docs/tests/agent-core-C.md):
- ``ReceiptRepository.link_items(receipt_id, links: Sequence[ItemLink]) -> Receipt`` sets
  ``product_id`` and ``match_source`` of the lines at the links' positions and returns the
  receipt. Line ids, the other lines and every other field are unchanged (``updated_at`` is
  stamped). NotFoundError for an unknown receipt or a position the receipt does not have, and
  then nothing is changed.
- ``PriceRepository.reassign(line_item_id, product_id) -> PriceObservation | None`` moves the
  line's observation to another product (everything else kept); None when the line has none.
  ``CorrectLineItem`` uses it ("moves its price observation", S4-AC-06).
- ``replace_contents`` drops the price observations of the lines it replaces (SQLite: the
  ``line_items`` foreign key cascades), so reprocessing a receipt never leaves stale prices.
"""

import pytest

from paper_trail.domain.enums import MatchSource
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import ItemLink, Receipt
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.factories import price_observation, product
from tests.storage import in_unit, seed_product_and_receipt
from tests.support import require_id


def _receipt(uow_factory: UnitOfWorkFactory, receipt_id: int) -> Receipt:
    with uow_factory(read_only=True) as uow:
        found = uow.receipts.get(receipt_id)
    assert found is not None
    return found


def test_link_items_links_the_given_lines_only(uow_factory: UnitOfWorkFactory) -> None:
    product_id, receipt_id, line_ids = seed_product_and_receipt(uow_factory)
    before = _receipt(uow_factory, receipt_id)

    returned = in_unit(
        uow_factory,
        lambda uow: uow.receipts.link_items(
            receipt_id, [ItemLink(position=1, product_id=product_id, match_source=MatchSource.NEW)]
        ),
    )

    stored = _receipt(uow_factory, receipt_id)
    assert returned == stored
    assert [require_id(item.id) for item in stored.items] == line_ids
    eggs, milk = stored.items
    assert (eggs.product_id, eggs.match_source) == (before.items[0].product_id, None)
    assert (milk.product_id, milk.match_source) == (product_id, MatchSource.NEW)
    assert (stored.total, stored.status, stored.created_at) == (
        before.total,
        before.status,
        before.created_at,
    )


def test_link_items_relinks_a_linked_line(uow_factory: UnitOfWorkFactory) -> None:
    first, receipt_id, _ = seed_product_and_receipt(uow_factory)
    second = require_id(in_unit(uow_factory, lambda uow: uow.products.add(product())).id)

    def link(uow: UnitOfWork, product_id: int, source: MatchSource) -> Receipt:
        return uow.receipts.link_items(receipt_id, [ItemLink(0, product_id, source)])

    in_unit(uow_factory, lambda uow: link(uow, first, MatchSource.ALIAS))
    in_unit(uow_factory, lambda uow: link(uow, second, MatchSource.MEMORY))

    eggs = _receipt(uow_factory, receipt_id).items[0]
    assert (eggs.product_id, eggs.match_source) == (second, MatchSource.MEMORY)


@pytest.mark.parametrize("receipt_offset, position", [(1, 0), (0, 9)], ids=["receipt", "line"])
def test_link_items_refuses_what_does_not_exist(
    uow_factory: UnitOfWorkFactory, receipt_offset: int, position: int
) -> None:
    product_id, receipt_id, _ = seed_product_and_receipt(uow_factory)
    links = [
        ItemLink(0, product_id, MatchSource.ALIAS),
        ItemLink(position, product_id, MatchSource.ALIAS),
    ]

    unknown = receipt_id + receipt_offset

    with pytest.raises(NotFoundError):
        in_unit(uow_factory, lambda uow: uow.receipts.link_items(unknown, links))

    assert _receipt(uow_factory, receipt_id).items[0].match_source is None


def test_reassign_moves_the_lines_price_observation(uow_factory: UnitOfWorkFactory) -> None:
    first, receipt_id, line_ids = seed_product_and_receipt(uow_factory)
    second = require_id(in_unit(uow_factory, lambda uow: uow.products.add(product())).id)
    in_unit(
        uow_factory,
        lambda uow: uow.prices.add_many(
            [price_observation(product_id=first, receipt_id=receipt_id, line_item_id=line_ids[0])]
        ),
    )

    moved = in_unit(uow_factory, lambda uow: uow.prices.reassign(line_ids[0], second))

    assert moved is not None
    assert (moved.product_id, moved.line_item_id) == (second, line_ids[0])
    with uow_factory(read_only=True) as uow:
        assert uow.prices.history(first) == ()
        assert uow.prices.history(second) == (moved,)


def test_reassign_without_an_observation_is_none(uow_factory: UnitOfWorkFactory) -> None:
    product_id, _, line_ids = seed_product_and_receipt(uow_factory)

    assert in_unit(uow_factory, lambda uow: uow.prices.reassign(line_ids[1], product_id)) is None


def test_replace_contents_drops_the_replaced_lines_prices(
    uow_factory: UnitOfWorkFactory,
) -> None:
    product_id, receipt_id, line_ids = seed_product_and_receipt(uow_factory)
    in_unit(
        uow_factory,
        lambda uow: uow.prices.add_many(
            [
                price_observation(
                    product_id=product_id, receipt_id=receipt_id, line_item_id=line_ids[0]
                )
            ]
        ),
    )
    current = _receipt(uow_factory, receipt_id)

    in_unit(uow_factory, lambda uow: uow.receipts.replace_contents(current))

    with uow_factory(read_only=True) as uow:
        assert uow.prices.history(product_id) == ()
