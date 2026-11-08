"""``UnitOfWork`` on SQLite: one connection and one transaction per unit.

A writing unit begins ``IMMEDIATE``; a read-only unit begins ``DEFERRED`` with ``query_only``.
"""

from typing import Self

from sqlalchemy import Connection, Engine, RootTransaction

from paper_trail.adapters.sqlite.correction_repository import SqliteCorrectionRepository
from paper_trail.adapters.sqlite.engine import begin_immediate, begin_read_only
from paper_trail.adapters.sqlite.job_queue import SqliteJobQueue
from paper_trail.adapters.sqlite.price_repository import SqlitePriceRepository
from paper_trail.adapters.sqlite.product_repository import SqliteProductRepository
from paper_trail.adapters.sqlite.receipt_repository import SqliteReceiptRepository
from paper_trail.adapters.sqlite.review_queue import SqliteReviewQueue
from paper_trail.adapters.sqlite.text_search import SqliteTextSearchIndex
from paper_trail.adapters.sqlite.vector_index import SqliteVectorIndex
from paper_trail.ports.clock import Clock
from paper_trail.ports.queues import JobQueue, ReviewQueue
from paper_trail.ports.repositories import (
    CorrectionRepository,
    PriceRepository,
    ProductRepository,
    ReceiptRepository,
)
from paper_trail.ports.search import TextSearchIndex, VectorIndex


class SqliteUnitOfWork:
    """Implements ``paper_trail.ports.unit_of_work.UnitOfWork``.

    Entering takes a pooled connection and begins a transaction. A writing unit begins an
    immediate one, so it never fails half-way on a lock upgrade; such units are kept short
    because they hold the database's write lock. A read-only unit begins a deferred one: in WAL
    mode it reads a consistent snapshot while a writer holds the lock, and any write through it
    fails. Leaving without ``commit()`` rolls everything back.
    """

    receipts: ReceiptRepository
    products: ProductRepository
    prices: PriceRepository
    corrections: CorrectionRepository
    jobs: JobQueue
    reviews: ReviewQueue
    text_search: TextSearchIndex
    vectors: VectorIndex

    def __init__(self, engine: Engine, clock: Clock, *, read_only: bool = False) -> None:
        self._engine = engine
        self._clock = clock
        self._read_only = read_only
        self._connection: Connection | None = None
        self._transaction: RootTransaction | None = None

    def __enter__(self) -> Self:
        connection = self._engine.connect()
        try:
            begin = begin_read_only if self._read_only else begin_immediate
            self._transaction = begin(connection)
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        self.receipts = SqliteReceiptRepository(connection, self._clock)
        self.products = SqliteProductRepository(connection)
        self.prices = SqlitePriceRepository(connection)
        self.corrections = SqliteCorrectionRepository(connection)
        self.jobs = SqliteJobQueue(connection, self._clock)
        self.reviews = SqliteReviewQueue(connection, self._clock)
        self.text_search = SqliteTextSearchIndex(connection)
        self.vectors = SqliteVectorIndex(connection)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._connection is not None:
            self._connection.close()  # rolls back whatever was not committed
        self._connection = self._transaction = None

    def commit(self) -> None:
        if self._transaction is None:
            msg = "commit() outside the unit of work's `with` block"
            raise RuntimeError(msg)
        if self._read_only:
            msg = "commit() in a read-only unit of work"
            raise RuntimeError(msg)
        self._transaction.commit()
