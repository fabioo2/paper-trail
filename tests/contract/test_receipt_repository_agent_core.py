"""ReceiptRepository additions of the agent core, for the fake and SQLite (PLAN 3.4, AC-A-02).

Pinned (docs/tests/agent-core-A.md): ``image_sha256`` and ``match_source`` round-trip through
``add``, ``get`` and ``replace_contents``; ``find_by_image_sha256`` finds the one receipt with
that image (None otherwise); a second receipt with the same image raises ConflictError (the
SQLite adapter translates the unique index violation).
"""

from dataclasses import replace

import pytest

from paper_trail.domain.enums import MatchSource
from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import Receipt
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import line_item, receipt
from tests.storage import in_unit
from tests.support import require_id

SHA = "0123456789abcdef" * 4


def _add(uow_factory: UnitOfWorkFactory, value: Receipt) -> Receipt:
    return in_unit(uow_factory, lambda uow: uow.receipts.add(value))


def test_image_sha256_and_match_source_round_trip(uow_factory: UnitOfWorkFactory) -> None:
    items = (
        line_item(match_source=MatchSource.ALIAS),
        line_item(position=1, raw_text="BAG", match_source=None),
    )
    saved = _add(uow_factory, receipt(image_sha256=SHA, items=items))

    with uow_factory(read_only=True) as uow:
        loaded = uow.receipts.get(require_id(saved.id))

    assert loaded is not None
    assert loaded.image_sha256 == SHA
    assert [item.match_source for item in loaded.items] == [MatchSource.ALIAS, None]


def test_find_by_image_sha256(uow_factory: UnitOfWorkFactory) -> None:
    _add(uow_factory, receipt())
    saved = _add(uow_factory, receipt(image_sha256=SHA))

    with uow_factory(read_only=True) as uow:
        assert uow.receipts.find_by_image_sha256(SHA) == saved
        assert uow.receipts.find_by_image_sha256("f" * 64) is None


def test_one_receipt_per_image(uow_factory: UnitOfWorkFactory) -> None:
    _add(uow_factory, receipt(image_sha256=SHA))

    with pytest.raises(ConflictError):
        _add(uow_factory, receipt(image_sha256=SHA))


def test_receipts_without_an_image_do_not_conflict(uow_factory: UnitOfWorkFactory) -> None:
    _add(uow_factory, receipt(image_sha256=None))
    _add(uow_factory, receipt(image_sha256=None))


def test_replace_contents_keeps_the_image_and_stores_match_sources(
    uow_factory: UnitOfWorkFactory,
) -> None:
    saved = _add(uow_factory, receipt(image_sha256=SHA, items=()))
    updated = replace(saved, items=(line_item(match_source=MatchSource.NEW),))

    stored = in_unit(uow_factory, lambda uow: uow.receipts.replace_contents(updated))

    assert stored.image_sha256 == SHA
    assert stored.items[0].match_source is MatchSource.NEW
    with uow_factory(read_only=True) as uow:
        assert uow.receipts.find_by_image_sha256(SHA) == stored
