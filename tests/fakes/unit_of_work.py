"""In-memory unit of work (implements ``paper_trail.ports.unit_of_work.UnitOfWork``).

Each unit works on a private deep copy of the shared ``FakeDatabase`` state and publishes it only
on ``commit()``: an exception or a missing commit leaves the database as it was, exactly like the
SQLite unit of work's transaction.
"""

import copy
from dataclasses import dataclass, field
from typing import Self

from paper_trail.ports.clock import Clock
from paper_trail.ports.queues import JobQueue, ReviewQueue
from paper_trail.ports.repositories import (
    CorrectionRepository,
    PriceRepository,
    ProductRepository,
    ReceiptRepository,
)
from paper_trail.ports.search import TextSearchIndex, VectorIndex
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.fakes.queues import FakeJobQueue, FakeReviewQueue
from tests.fakes.repositories import (
    FakeCorrectionRepository,
    FakePriceRepository,
    FakeProductRepository,
    FakeReceiptRepository,
)
from tests.fakes.search import FakeTextSearchIndex, FakeVectorIndex
from tests.fakes.state import FakeState


@dataclass
class FakeDatabase:
    """The committed state shared by every unit of work of one test."""

    state: FakeState = field(default_factory=FakeState)
    commits: int = 0


class FakeUnitOfWork:
    """A read-only unit refuses ``commit()``; its writes stay in its private copy and vanish."""

    def __init__(self, database: FakeDatabase, clock: Clock, *, read_only: bool = False) -> None:
        self._database = database
        self._read_only = read_only
        working = copy.deepcopy(database.state)
        self._working = working
        # Annotated with the port types: mypy then checks each fake against its protocol.
        self.receipts: ReceiptRepository = FakeReceiptRepository(working, clock)
        self.products: ProductRepository = FakeProductRepository(working)
        self.prices: PriceRepository = FakePriceRepository(working)
        self.corrections: CorrectionRepository = FakeCorrectionRepository(working)
        self.jobs: JobQueue = FakeJobQueue(working, clock)
        self.reviews: ReviewQueue = FakeReviewQueue(working, clock)
        self.text_search: TextSearchIndex = FakeTextSearchIndex(working)
        self.vectors: VectorIndex = FakeVectorIndex(working)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        """Nothing to roll back: uncommitted changes live only in this unit's copy."""

    def commit(self) -> None:
        if self._read_only:
            msg = "commit() in a read-only unit of work"
            raise RuntimeError(msg)
        self._database.state = copy.deepcopy(self._working)
        self._database.commits += 1


def fake_uow_factory(database: FakeDatabase, clock: Clock) -> UnitOfWorkFactory:
    def factory(*, read_only: bool = False) -> FakeUnitOfWork:
        return FakeUnitOfWork(database, clock, read_only=read_only)

    return factory
