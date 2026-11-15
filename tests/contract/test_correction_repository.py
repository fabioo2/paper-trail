"""CorrectionRepository contract (S1-AC-10), for the fake and the SQLite adapter.

Pinned (docs/tests/stage1.md): ``latest`` matches ``store_key`` exactly (None only matches None)
and breaks created_at ties by the higher id. Barcode-lookup PR 1b (the backfill's A8 key
migration): ``list`` in id order (of one kind when given), ``update`` replaces every field,
``update`` of an unknown id raises NotFoundError.
"""

from dataclasses import replace

import pytest

from paper_trail.domain.enums import CorrectionKind
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import Correction
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import at, correction
from tests.storage import in_unit, seed_product_and_receipt


def _add_correction(uow_factory: UnitOfWorkFactory, value: Correction) -> Correction:
    return in_unit(uow_factory, lambda uow: uow.corrections.add(value))


def _latest(
    uow_factory: UnitOfWorkFactory, kind: CorrectionKind, subject: str, store_key: str | None
) -> Correction | None:
    with uow_factory() as uow:
        return uow.corrections.latest(kind, subject, store_key)


def test_s1_ac_10_correction_add_round_trips(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: add assigns an id; latest returns the stored correction."""
    _, receipt_id, (line_id, _) = seed_product_and_receipt(uow_factory)
    original = correction(receipt_id=receipt_id, line_item_id=line_id, old_value=None)

    stored = _add_correction(uow_factory, original)

    assert stored.id is not None
    assert replace(stored, id=None) == original
    assert _latest(uow_factory, original.kind, original.subject, original.store_key) == stored


def test_s1_ac_10_latest_returns_the_newest_matching_correction(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """S1-AC-10: newest by created_at, ties by higher id; kind, subject and store must match."""
    _add_correction(uow_factory, correction(new_value="produce", created_at=at(0)))
    newest = _add_correction(uow_factory, correction(new_value="dairy_eggs", created_at=at(5)))
    _add_correction(uow_factory, correction(new_value="pantry", created_at=at(1)))
    _add_correction(uow_factory, correction(kind=CorrectionKind.FIELD, created_at=at(9)))
    _add_correction(uow_factory, correction(subject="other", created_at=at(9)))
    _add_correction(uow_factory, correction(store_key="walmart", created_at=at(9)))
    _add_correction(uow_factory, correction(store_key=None, created_at=at(9)))
    tie = _add_correction(uow_factory, correction(new_value="frozen", created_at=at(5)))

    found = _latest(uow_factory, CorrectionKind.CATEGORY, "ks org eggs 24ct", "costco")

    assert found == tie
    assert found != newest


def test_s1_ac_10_latest_with_no_store_matches_only_storeless(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Pinned: store_key=None matches corrections recorded without a store, nothing else."""
    _add_correction(uow_factory, correction(store_key="costco"))
    assert _latest(uow_factory, CorrectionKind.CATEGORY, "ks org eggs 24ct", None) is None

    storeless = _add_correction(uow_factory, correction(store_key=None))
    assert _latest(uow_factory, CorrectionKind.CATEGORY, "ks org eggs 24ct", None) == storeless


def test_s1_ac_10_latest_without_match_is_none(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: nothing recorded, nothing found."""
    assert _latest(uow_factory, CorrectionKind.PRODUCT_LINK, "anything", "costco") is None


def test_list_is_every_correction_in_id_order_filtered_by_kind(
    uow_factory: UnitOfWorkFactory,
) -> None:
    first = _add_correction(uow_factory, correction(created_at=at(5)))
    link = _add_correction(
        uow_factory, correction(kind=CorrectionKind.PRODUCT_LINK, new_value="1", created_at=at(0))
    )
    third = _add_correction(uow_factory, correction(store_key=None))

    with uow_factory() as uow:
        assert uow.corrections.list() == (first, link, third)
        assert uow.corrections.list(CorrectionKind.PRODUCT_LINK) == (link,)
        assert uow.corrections.list(CorrectionKind.FIELD) == ()


def test_update_replaces_every_field(uow_factory: UnitOfWorkFactory) -> None:
    kept = _add_correction(uow_factory, correction(subject="tst apple mrj"))
    other = _add_correction(uow_factory, correction(subject="other"))
    changed = replace(kept, subject="tst apple", new_value="pantry", created_at=at(9))

    with uow_factory() as uow:
        assert uow.corrections.update(changed) == changed
        uow.commit()

    with uow_factory() as uow:
        assert uow.corrections.list() == (changed, other)


def test_update_of_an_unknown_correction_is_not_found(uow_factory: UnitOfWorkFactory) -> None:
    with uow_factory() as uow, pytest.raises(NotFoundError):
        uow.corrections.update(replace(correction(), id=99))
