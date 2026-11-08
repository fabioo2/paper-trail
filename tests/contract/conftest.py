"""Port contracts run against the in-memory fakes and the real adapters (S1-AC-10).

Every storage port is reached through a ``UnitOfWork``, exactly as the application reaches it.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.fakes.clock import FakeClock
from tests.fakes.unit_of_work import FakeDatabase, fake_uow_factory
from tests.storage import sqlite_uow_factory
from tests.support import CopyMigrated

UOW_IMPLEMENTATIONS = ("fake", "sqlite")


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(params=UOW_IMPLEMENTATIONS)
def uow_factory(
    request: pytest.FixtureRequest,
    fake_clock: FakeClock,
    tmp_path: Path,
    copy_migrated: CopyMigrated,
) -> Iterator[UnitOfWorkFactory]:
    if request.param == "fake":
        yield fake_uow_factory(FakeDatabase(), fake_clock)
        return
    engine = create_engine(copy_migrated(tmp_path / "contract.db"))
    try:
        yield sqlite_uow_factory(engine, fake_clock)
    finally:
        engine.dispose()
