"""Composition root: the only place that chooses adapters for the ports (ADR-0002).

This module wires the application core (database, use cases, health): what ``serve``,
``worker`` and ``db`` need. The agents are wired in ``paper_trail.bootstrap.agents``, which
imports LangGraph, the MCP SDK and the Ollama and Tesseract adapters; it is a separate module so
the commands that run no agent never pay for importing them (CLI start-up).
"""

from dataclasses import dataclass

from sqlalchemy import Engine

from paper_trail.adapters.catalog.composite import CompositeCatalog
from paper_trail.adapters.catalog.plu import PluCatalog
from paper_trail.adapters.clock import SystemClock
from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.health import DatabaseHealthProbe
from paper_trail.adapters.sqlite.migrations import current_revision, head_revision, upgrade_to_head
from paper_trail.adapters.sqlite.store_catalog import StoreCatalog
from paper_trail.adapters.sqlite.unit_of_work import SqliteUnitOfWork
from paper_trail.application.catalog import (
    AddCatalogCode,
    BackfillCatalog,
    CatalogTodo,
    ListCatalogCodes,
    LookupCatalogCode,
    RemoveCatalogCode,
)
from paper_trail.application.corrections import CorrectLineItem
from paper_trail.application.health import CheckReadiness
from paper_trail.application.prices import BackfillPrices
from paper_trail.application.products import GetProduct
from paper_trail.application.receipts import (
    GetReceipt,
    ListReceipts,
    RecordReceipt,
)
from paper_trail.config import Settings
from paper_trail.domain.rules.arithmetic import Tolerances
from paper_trail.ports.clock import Clock
from paper_trail.ports.unit_of_work import UnitOfWorkFactory

IMAGES_DIRNAME = "images"
RUNS_DIRNAME = "runs"


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    clock: Clock
    engine: Engine
    uow_factory: UnitOfWorkFactory
    check_readiness: CheckReadiness
    record_receipt: RecordReceipt
    get_receipt: GetReceipt
    list_receipts: ListReceipts
    correct_line_item: CorrectLineItem
    get_product: GetProduct
    backfill_prices: BackfillPrices
    catalog_todo: CatalogTodo
    add_catalog_code: AddCatalogCode
    list_catalog_codes: ListCatalogCodes
    remove_catalog_code: RemoveCatalogCode
    lookup_catalog_code: LookupCatalogCode
    backfill_catalog: BackfillCatalog
    tolerances: Tolerances


def build_container(settings: Settings) -> Container:
    """Wire the application. Performs no I/O: the database is opened on first use."""
    engine = create_engine(settings.database_path)
    clock = SystemClock()
    tolerances = Tolerances()  # one policy for recording and for reading back

    def uow_factory(*, read_only: bool = False) -> SqliteUnitOfWork:
        return SqliteUnitOfWork(engine, clock, read_only=read_only)

    if settings.catalog.plu_path is None:
        msg = "Settings fills in CATALOG__PLU_PATH"
        raise ValueError(msg)
    plu = PluCatalog(settings.catalog.plu_path)

    return Container(
        settings=settings,
        clock=clock,
        engine=engine,
        uow_factory=uow_factory,
        check_readiness=CheckReadiness([DatabaseHealthProbe(engine)]),
        record_receipt=RecordReceipt(uow_factory, clock, tolerances),
        get_receipt=GetReceipt(uow_factory, tolerances),
        list_receipts=ListReceipts(uow_factory),
        correct_line_item=CorrectLineItem(uow_factory, clock),
        get_product=GetProduct(uow_factory),
        backfill_prices=BackfillPrices(uow_factory),
        catalog_todo=CatalogTodo(uow_factory),
        add_catalog_code=AddCatalogCode(uow_factory, clock),
        list_catalog_codes=ListCatalogCodes(uow_factory),
        remove_catalog_code=RemoveCatalogCode(uow_factory),
        lookup_catalog_code=LookupCatalogCode(CompositeCatalog(StoreCatalog(uow_factory), plu)),
        backfill_catalog=BackfillCatalog(uow_factory, clock, plu),
        tolerances=tolerances,
    )


def dispose(container: Container) -> None:
    container.engine.dispose()


def upgrade_database(container: Container) -> None:
    """Apply every pending migration (``paper-trail db upgrade``)."""
    upgrade_to_head(container.engine)


def database_revision(container: Container) -> str | None:
    """The schema revision the database is at (``paper-trail db current``)."""
    return current_revision(container.engine)


def database_is_current(container: Container) -> bool:
    """Whether the database exists and is migrated to the newest schema."""
    return container.settings.database_path.is_file() and (
        database_revision(container) == head_revision()
    )
