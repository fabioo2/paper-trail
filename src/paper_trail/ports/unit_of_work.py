"""One transaction across every storage port (PLAN 3.4)."""

from typing import Protocol, Self

from paper_trail.ports.queues import JobQueue, ReviewQueue
from paper_trail.ports.repositories import (
    CorrectionRepository,
    PriceRepository,
    ProductRepository,
    ReceiptRepository,
)
from paper_trail.ports.search import TextSearchIndex, VectorIndex


class UnitOfWork(Protocol):
    """Use as ``with factory() as uow: ...; uow.commit()``.

    Leaving the block without ``commit()``, or with an exception (which propagates), undoes
    every write made through the unit. A unit reads its own uncommitted writes.

    A read-only unit (``factory(read_only=True)``) sees one consistent snapshot without taking
    the write lock, so it never waits for, nor holds up, a writer; writing through it is an
    error and so is ``commit()``.
    """

    receipts: ReceiptRepository
    products: ProductRepository
    prices: PriceRepository
    corrections: CorrectionRepository
    jobs: JobQueue
    reviews: ReviewQueue
    text_search: TextSearchIndex
    vectors: VectorIndex

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc: object) -> None: ...

    def commit(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    """Opens a unit of work; a writing one by default, a read-only one on request."""

    def __call__(self, *, read_only: bool = False) -> UnitOfWork: ...
