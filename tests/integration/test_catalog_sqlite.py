"""``catalog todo`` and ``catalog add`` on a real database file (barcode-lookup plan R15, R16;
PC-TC-106, 117).

Pinned: ``todo``'s counts agree with the stored lines and observations (a DISCOUNT line linked
to the same product is no purchase); ``add`` writes the product and its code in one unit of
work, so a failing code write leaves nothing, and a repeat writes nothing.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.product_code_repository import SqliteProductCodeRepository
from paper_trail.application.catalog import AddCatalogCode, CatalogTodo, CodeNaming
from paper_trail.domain.enums import CodeScheme, CodeSource, FieldSource, MatchSource
from paper_trail.domain.models import ProductCode
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.extractions import C1, C2, L1, LOBLAWS
from tests.factories import CREATED_AT, STORE_NAME, discount_line, item_line, product
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, seed_purchase, sqlite_uow_factory
from tests.support import CopyMigrated, require_id

TABLES = (
    "receipts",
    "products",
    "product_codes",
    "product_aliases",
    "line_items",
    "price_observations",
    "corrections",
)
LATEST = (
    "SELECT paid_cents, observed_on FROM price_observations WHERE product_id = ?"
    " ORDER BY observed_on DESC, id DESC LIMIT 1"
)


class CodeWriteError(RuntimeError):
    """The product-code repository failed after the product was written."""


@dataclass(frozen=True)
class Store:
    path: Path
    factory: UnitOfWorkFactory
    clock: FakeClock


@pytest.fixture
def store(tmp_path: Path, copy_migrated: CopyMigrated) -> Iterator[Store]:
    path = copy_migrated(tmp_path / "catalog.db")
    engine: Engine = create_engine(path)
    clock = FakeClock()
    try:
        yield Store(path, sqlite_uow_factory(engine, clock), clock)
    finally:
        engine.dispose()


def _snapshot(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()  # noqa: S608
            for table in TABLES
        }


def _printed(factory: UnitOfWorkFactory, name: str, code: str, store_key: str = "costco") -> int:
    """A printed-text product with its code (source ``receipt``); its id."""
    scheme = CodeScheme.STORE if store_key else CodeScheme.GTIN

    def store(uow: Any) -> int:
        pid = require_id(
            uow.products.add(
                product(
                    canonical_name=name,
                    brand=None,
                    size_value=None,
                    size_unit=None,
                    category=None,
                    tags=(),
                    field_sources={"canonical_name": FieldSource.RECEIPT_TEXT},
                )
            ).id
        )
        uow.product_codes.upsert(
            ProductCode(
                None, pid, scheme, store_key, code, CodeSource.RECEIPT, CREATED_AT, CREATED_AT
            ),
            replace=False,
        )
        return pid

    return in_unit(factory, store)


def _bought(  # noqa: PLR0913, PLR0917 - a test builder: one receipt line and its discount
    factory: UnitOfWorkFactory, store_name: str, day: int, text: str, pid: int, discount: int = 0
) -> None:
    """One line of ``text`` for 16.99 on 2026-11-``day``; with ``discount``, a DISCOUNT line
    linked to it that also carries the product (a DISCOUNT line is no purchase)."""
    lines = [item_line(0, text, 1699, product_id=pid, match_source=MatchSource.CATALOG)]
    if discount:
        lines.append(discount_line(1, "TPD/1362394", -discount, 0, product_id=pid))
    seed_purchase(factory, lines, store_name=store_name, purchased_on=date(2026, 11, day))


def test_pc_tc_106_the_numbers_agree_with_the_stored_lines(store: Store) -> None:
    choc = _printed(store.factory, "CHOC PISTACH", "1362394")
    kiwi = _printed(store.factory, "GOLD KIWI", "1543210")
    wipes = _printed(store.factory, "PC UNS BABYWIPES", "0060383064877", store_key="")
    for day in (1, 2):
        _bought(store.factory, STORE_NAME, day, C1, choc)
    _bought(store.factory, STORE_NAME, 3, C1, choc, discount=400)
    _bought(store.factory, STORE_NAME, 2, C2, kiwi)
    _bought(store.factory, LOBLAWS, 1, L1, wipes)
    _bought(store.factory, LOBLAWS, 4, L1, wipes)

    found = CatalogTodo(store.factory)()

    with closing(sqlite3.connect(store.path)) as connection:
        stored = [
            (
                connection.execute(
                    "SELECT count(*) FROM line_items WHERE product_id = ? AND kind = 'item'",
                    (pid,),
                ).fetchone()[0],
                *connection.execute(LATEST, (pid,)).fetchone(),
            )
            for pid in (choc, wipes, kiwi)
        ]
    assert [
        (row.times_bought, row.last_paid and row.last_paid.amount_cents, str(row.last_date))
        for row in found
    ] == stored
    assert [row.product_id for row in found] == [choc, wipes, kiwi]
    assert stored == [(3, 1299, "2026-11-03"), (2, 1699, "2026-11-04"), (1, 1699, "2026-11-02")]


def test_pc_tc_117_a_failing_code_write_leaves_nothing_and_a_repeat_writes_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    choc = _printed(store.factory, "CHOC PISTACH", "1362394")
    _bought(store.factory, STORE_NAME, 1, C1, choc)
    before = _snapshot(store.path)
    upsert = SqliteProductCodeRepository.upsert

    def failing(self: SqliteProductCodeRepository, code: ProductCode, *, replace: bool) -> Any:
        raise CodeWriteError(code.code)

    monkeypatch.setattr(SqliteProductCodeRepository, "upsert", failing)
    store.clock.advance(60)
    add = AddCatalogCode(store.factory, store.clock)
    naming = CodeNaming("costco", "1362394", name="Choc Pistachio Bars")

    with pytest.raises(CodeWriteError):
        add(naming)

    assert _snapshot(store.path) == before
    monkeypatch.setattr(SqliteProductCodeRepository, "upsert", upsert)
    assert add(naming).changed is True
    after = _snapshot(store.path)
    store.clock.advance(60)
    assert add(naming).changed is False
    assert _snapshot(store.path) == after
