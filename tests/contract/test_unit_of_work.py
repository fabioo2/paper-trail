"""UnitOfWork contract (S1-AC-18): commit only on commit(), for the fake and SQLite units."""

import pytest

from paper_trail.domain.enums import JobKind
from paper_trail.domain.paging import PageRequest
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import product, receipt
from tests.storage import in_unit
from tests.support import require_id


class _BoomError(Exception):
    """Raised inside a unit of work to abort it."""


def _receipt_count(uow_factory: UnitOfWorkFactory) -> int:
    with uow_factory() as uow:
        return len(uow.receipts.list(PageRequest(limit=100)).items)


def test_s1_ac_18_commit_makes_changes_visible(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-18: changes committed in one unit are seen by the next."""
    saved = in_unit(uow_factory, lambda uow: uow.receipts.add(receipt()))

    with uow_factory() as uow:
        assert uow.receipts.get(require_id(saved.id)) == saved


def test_s1_ac_18_missing_commit_rolls_back(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-18: leaving the block without commit() leaves the database unchanged."""
    with uow_factory() as uow:
        uow.receipts.add(receipt())
        uow.jobs.pending_count()

    assert _receipt_count(uow_factory) == 0


def test_s1_ac_18_exception_rolls_back_and_propagates(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-18: an exception inside the block rolls everything back and is re-raised."""
    with pytest.raises(_BoomError), uow_factory() as uow:
        uow.receipts.add(receipt())
        uow.receipts.add(receipt())
        raise _BoomError

    assert _receipt_count(uow_factory) == 0


def test_s1_ac_18_writes_before_commit_are_visible_inside_the_unit(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """S1-AC-18: a unit reads its own uncommitted writes (one transaction per unit)."""
    with uow_factory() as uow:
        saved = uow.receipts.add(receipt())
        assert uow.receipts.get(require_id(saved.id)) == saved


def test_s1_ac_18_one_unit_is_all_or_nothing_across_repositories(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """S1-AC-18: a failure after writes to several repositories undoes all of them."""
    with pytest.raises(_BoomError), uow_factory() as uow:
        saved = uow.receipts.add(receipt())
        uow.products.add(product())
        uow.jobs.enqueue(JobKind.PROCESS_RECEIPT, require_id(saved.id), max_attempts=3)
        raise _BoomError

    with uow_factory() as uow:
        assert uow.products.get(1) is None
        assert uow.jobs.pending_count() == 0
    assert _receipt_count(uow_factory) == 0


def test_s1_ac_18_a_read_only_unit_sees_committed_data(uow_factory: UnitOfWorkFactory) -> None:
    """PLAN 3.4: a read-only unit reads what earlier units committed."""
    saved = in_unit(uow_factory, lambda uow: uow.receipts.add(receipt()))

    with uow_factory(read_only=True) as uow:
        assert uow.receipts.get(require_id(saved.id)) == saved


def test_s1_ac_18_a_read_only_unit_cannot_commit(uow_factory: UnitOfWorkFactory) -> None:
    """PLAN 3.4: commit() in a read-only unit is a programming error, and nothing is stored."""
    with pytest.raises(RuntimeError, match="read-only"), uow_factory(read_only=True) as uow:
        uow.commit()

    assert _receipt_count(uow_factory) == 0
