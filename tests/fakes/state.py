"""The in-memory "database" behind the fake repositories, queues and indexes.

``FakeState`` is plain data; ``tests.fakes.unit_of_work.FakeUnitOfWork`` gives each unit a deep
copy and publishes it on ``commit()``, which mirrors a transaction's all-or-nothing behaviour.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from paper_trail.domain.models import (
    Correction,
    Job,
    PriceObservation,
    Product,
    ProductAlias,
    Receipt,
    ReviewItem,
)
from paper_trail.domain.paging import Page, PageRequest, decode_cursor, encode_cursor
from tests.support import require_id


@dataclass
class FakeState:
    receipts: dict[int, Receipt] = field(default_factory=dict)
    products: dict[int, Product] = field(default_factory=dict)
    aliases: dict[int, ProductAlias] = field(default_factory=dict)
    prices: dict[int, PriceObservation] = field(default_factory=dict)
    corrections: dict[int, Correction] = field(default_factory=dict)
    jobs: dict[int, Job] = field(default_factory=dict)
    reviews: dict[int, ReviewItem] = field(default_factory=dict)
    vectors: dict[int, tuple[float, ...]] = field(default_factory=dict)
    _sequences: defaultdict[str, int] = field(default_factory=lambda: defaultdict(int))

    def next_id(self, table: str) -> int:
        """Like INTEGER PRIMARY KEY: ids start at 1 and are never reused."""
        self._sequences[table] += 1
        return self._sequences[table]


class _Pageable(Protocol):
    @property
    def id(self) -> int | None: ...

    @property
    def created_at(self) -> datetime: ...


def newest_first_page[T, R: _Pageable](
    rows: Iterable[R], page: PageRequest, to_item: Callable[[R], T]
) -> Page[T]:
    """Keyset pagination newest first (created_at desc, id desc), as the SQLite adapters do."""
    ordered = sorted(rows, key=lambda row: (row.created_at, require_id(row.id)), reverse=True)
    if page.cursor is not None:
        after = decode_cursor(page.cursor)
        ordered = [row for row in ordered if (row.created_at, require_id(row.id)) < after]
    window = ordered[: page.limit + 1]
    has_more = len(window) > page.limit
    shown = window[: page.limit]
    next_cursor = (
        encode_cursor(shown[-1].created_at, require_id(shown[-1].id)) if has_more else None
    )
    return Page(items=tuple(to_item(row) for row in shown), next_cursor=next_cursor)
