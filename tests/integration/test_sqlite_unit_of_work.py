"""SQLite specifics of the unit of work and repositories that the port contracts leave out."""

import time

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from paper_trail.adapters.sqlite import unit_of_work as unit_of_work_module
from paper_trail.adapters.sqlite.unit_of_work import SqliteUnitOfWork
from paper_trail.application.receipts import GetReceipt, ListReceipts
from paper_trail.domain.enums import ReviewReason
from paper_trail.domain.paging import PageRequest
from paper_trail.domain.rules.arithmetic import Tolerances
from tests.factories import receipt
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, sqlite_uow_factory
from tests.support import require_id

# Far below busy_timeout (5000 ms): a read that queued behind the writer would take that long.
_UNBLOCKED_SECONDS = 1.0


class _LockedError(Exception):
    """Stands in for "database is locked" when the transaction cannot begin."""


def test_s1_ac_18_commit_outside_the_block_is_an_error(migrated_engine: Engine) -> None:
    """commit() needs the unit's transaction: before entering or after leaving it is a bug."""
    unit = SqliteUnitOfWork(migrated_engine, FakeClock())

    with pytest.raises(RuntimeError, match="outside"):
        unit.commit()
    with unit:
        pass
    with pytest.raises(RuntimeError, match="outside"):
        unit.commit()


def test_s1_ac_18_a_transaction_that_cannot_begin_returns_its_connection(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If BEGIN IMMEDIATE fails, entering raises and the pooled connection is given back."""

    def locked(*_: object) -> None:
        raise _LockedError

    monkeypatch.setattr(unit_of_work_module, "begin_immediate", locked)

    with pytest.raises(_LockedError), SqliteUnitOfWork(migrated_engine, FakeClock()):
        pass

    assert migrated_engine.pool.checkedout() == 0  # type: ignore[attr-defined]


def test_s1_ac_15_only_unique_violations_become_conflicts(migrated_engine: Engine) -> None:
    """A review item for a receipt that does not exist is a bug (foreign key), not a conflict."""
    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY"),
        SqliteUnitOfWork(migrated_engine, FakeClock()) as uow,
    ):
        uow.reviews.open(404, ReviewReason.LOW_CONFIDENCE, {}, None)


def test_s1_ac_10_replace_contents_needs_a_stored_receipt(migrated_engine: Engine) -> None:
    """replace_contents overwrites a stored receipt; one without an id is a programming error."""
    with (
        pytest.raises(ValueError, match="stored receipt"),
        SqliteUnitOfWork(migrated_engine, FakeClock()) as uow,
    ):
        uow.receipts.replace_contents(receipt())


def test_s1_ac_18_reads_do_not_wait_for_a_writer(migrated_engine: Engine) -> None:
    """ADR-0004: GetReceipt and ListReceipts read their WAL snapshot while a unit holds the lock.

    The writing unit is still open (BEGIN IMMEDIATE taken, its receipt uncommitted) when the
    reads run on other pooled connections; they neither wait for it nor see its write.
    """
    factory = sqlite_uow_factory(migrated_engine, FakeClock())
    stored = in_unit(factory, lambda uow: uow.receipts.add(receipt()))
    get_receipt, list_receipts = GetReceipt(factory, Tolerances()), ListReceipts(factory)

    with factory() as writer:
        writer.receipts.add(receipt())
        started = time.monotonic()
        loaded, _ = get_receipt(require_id(stored.id))
        listed = list_receipts(PageRequest(limit=10), None, None)
        elapsed = time.monotonic() - started

    assert elapsed < _UNBLOCKED_SECONDS
    assert loaded == stored
    assert [summary.id for summary in listed.items] == [stored.id]


def test_s1_ac_18_a_read_only_unit_cannot_write(migrated_engine: Engine) -> None:
    """A write through a read-only unit fails (query_only) instead of taking the write lock."""
    with (
        pytest.raises(OperationalError, match="readonly"),
        SqliteUnitOfWork(migrated_engine, FakeClock(), read_only=True) as uow,
    ):
        uow.receipts.add(receipt())


def test_s1_ac_18_a_pooled_connection_does_not_stay_read_only(migrated_engine: Engine) -> None:
    """query_only is per connection: the next unit on the same pooled connection can write."""
    migrated_engine.dispose()  # an empty pool: the read-only unit's connection is the only one
    with SqliteUnitOfWork(migrated_engine, FakeClock(), read_only=True) as uow:
        uow.receipts.list(PageRequest(limit=1))
    assert migrated_engine.pool.checkedin() == 1  # type: ignore[attr-defined]

    factory = sqlite_uow_factory(migrated_engine, FakeClock())
    saved = in_unit(factory, lambda uow: uow.receipts.add(receipt()))

    assert migrated_engine.pool.checkedin() == 1  # type: ignore[attr-defined]
    assert saved.id is not None
