"""The receipt graph wired from fakes and SQLite, the way ``bootstrap`` wires it from adapters.

Every graph-level test (unit, integration, contract) builds its system under test here, so the
production constructors are named in exactly one test module:

- ``RecordReceipt(uow_factory, clock, tolerances)`` (Stage 1, gains ``replace``)
- ``build_tool_registry(ToolServices(...))`` and ``InProcessToolGateway(registry)``; the
  tools' catalog is ``CompositeCatalog(StoreCatalog(uow_factory), catalog)``
- ``LangGraphReceiptRunner(AgentDeps(...), checkpointer)``: applies the tracing decorators itself;
  slice D adds ``AgentDeps.images`` (the image store the vision attempt reads the photo from)
- ``ProcessReceiptFile(image_store, uow_factory, runner, tracer, clock, hold=hold)``
- ``ResolveReview(uow_factory, runner, record_receipt, clock, hold=hold)``, both holding the
  receipt with ``receipt_hold(uow_factory)`` (``ReceiptHold``) while they run it
- ``CorrectLineItem(uow_factory, clock)`` (slice C)

Claude is always a ``ScriptedReader`` (claude-extraction plan R3: no test calls the API); the
runner wraps it in ``TracingReader`` per receipt itself. The checkpointer is LangGraph's
in-memory saver unless a test passes a ``SqliteSaver`` (integration).

The tool transport (S5-AC-04): ``build_harness`` puts ``InProcessToolGateway(registry)`` in
front of the registry, or, inside ``tool_transport("mcp")``, ``McpToolGateway(McpSettings)``
talking to the production MCP app served over that harness's registry on a loopback port
(``tests/mcp_server.py``). Graph suites run in-process; ``tests/unit/agents/
test_transport_equivalence.py`` runs representative scenarios over both and compares them.
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import cache
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from paper_trail.adapters.catalog.composite import CompositeCatalog
from paper_trail.adapters.mcp.gateway import McpToolGateway
from paper_trail.adapters.sqlite.store_catalog import StoreCatalog
from paper_trail.agents.graph import AgentDeps
from paper_trail.agents.prompts import PromptLibrary
from paper_trail.agents.runner import LangGraphReceiptRunner
from paper_trail.application.corrections import CorrectLineItem
from paper_trail.application.enrichment import ProductEnricher, Throttle
from paper_trail.application.holds import ReceiptHold
from paper_trail.application.process_receipt import (
    ProcessOutcome,
    ProcessReceiptFile,
    ReceiptSource,
)
from paper_trail.application.receipts import RecordReceipt, ResolveReview
from paper_trail.config import McpSettings, Settings
from paper_trail.domain.enums import CorrectionKind, MatchSource
from paper_trail.domain.models import (
    Correction,
    LineItem,
    PriceObservation,
    Product,
    ProductAlias,
    Receipt,
    ReviewItem,
)
from paper_trail.domain.paging import PageRequest
from paper_trail.domain.rules.arithmetic import Tolerances
from paper_trail.domain.trace import TraceEvent, TraceKind
from paper_trail.ports.catalog import ProductCatalog
from paper_trail.ports.lookups import ProductLookup
from paper_trail.ports.reader import ReceiptReader
from paper_trail.ports.tools import ToolGateway
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from paper_trail.tools.handlers import ToolServices
from paper_trail.tools.inprocess import InProcessToolGateway
from paper_trail.tools.registry import ToolRegistry, build_tool_registry
from tests.extractions import NOW
from tests.fakes.catalog import FakeCatalog
from tests.fakes.clock import FakeClock
from tests.fakes.images import FakeImageStore, fake_png
from tests.fakes.trace import FakeTracer
from tests.mcp_server import serve_mcp
from tests.storage import scratch_database

EXTRACT_MODEL = "claude-haiku-5-5"
RETRY_MODEL = "claude-opus-5-5"
TOOL_TRANSPORTS = ("inprocess", "mcp")
MCP_TIMEOUT_S = 10.0

CLI_HOLDER = "cli-test"

type GatewayFactory = Callable[[ToolRegistry], ToolGateway]


def receipt_hold(uow_factory: UnitOfWorkFactory) -> ReceiptHold:
    """The CLI's hold on a receipt it runs, as ``bootstrap.agents.receipt_hold`` builds it."""
    return ReceiptHold(uow_factory, holder=CLI_HOLDER, renew_every_s=10.0)


def mcp_gateway(base_url: str) -> ToolGateway:
    """The production MCP gateway pointed at a server's base URL."""
    return McpToolGateway(McpSettings.model_validate({"url": base_url, "timeout_s": MCP_TIMEOUT_S}))


@dataclass
class _ToolTransport:
    gateway: GatewayFactory = InProcessToolGateway


_TRANSPORT = _ToolTransport()


@contextmanager
def tool_transport(name: str) -> Iterator[None]:
    """Within the block every new harness calls its tools over ``name`` ("inprocess"|"mcp")."""
    assert name in TOOL_TRANSPORTS, name
    previous = _TRANSPORT.gateway
    with ExitStack() as servers:

        def over_mcp(registry: ToolRegistry) -> ToolGateway:
            return mcp_gateway(servers.enter_context(serve_mcp(registry)))

        _TRANSPORT.gateway = InProcessToolGateway if name == "inprocess" else over_mcp
        try:
            yield
        finally:
            _TRANSPORT.gateway = previous


def _scratch_uow_factory(clock: FakeClock) -> UnitOfWorkFactory:
    """A ``scratch_database`` made on the first unit of work: a harness built at import time
    only to list its tools never opens one."""
    database = cache(lambda: scratch_database(clock))

    def factory(*, read_only: bool = False) -> UnitOfWork:
        return database().uow_factory(read_only=read_only)

    return factory


def agent_settings(
    *,
    max_attempts: int = 2,
    feedback: bool = True,
    enrichment: bool = False,
    catalog: bool = True,
) -> Settings:
    """Settings for graph tests: ``GRAPH__MAX_ATTEMPTS``, ``GRAPH__FEEDBACK``,
    ``GRAPH__ENRICHMENT`` (off unless a test is about it) and ``CATALOG__ENABLED``; Claude's
    models are the defaults (``EXTRACT_MODEL``, then ``RETRY_MODEL``)."""
    return Settings.model_validate(
        {
            "env": "test",
            "graph": {
                "max_attempts": max_attempts,
                "feedback": feedback,
                "enrichment": enrichment,
            },
            "catalog": {"enabled": catalog},
        }
    )


@dataclass
class Harness:
    clock: FakeClock
    uow_factory: UnitOfWorkFactory
    images: FakeImageStore
    reader: ReceiptReader
    tracer: FakeTracer
    settings: Settings
    registry: ToolRegistry
    deps: AgentDeps
    checkpointer: BaseCheckpointSaver[Any]
    runner: LangGraphReceiptRunner
    process: ProcessReceiptFile
    resolve: ResolveReview
    record: RecordReceipt
    correct: CorrectLineItem

    def process_image(
        self,
        label: str = "costco-2026-11-01",
        *,
        reprocess: bool = False,
        interrupt_after: str | None = None,
    ) -> ProcessOutcome:
        return self.process(
            ReceiptSource(name=f"{label}.jpg", image=fake_png(label)),
            reprocess=reprocess,
            interrupt_after=interrupt_after,
        )

    def resume(self, receipt_id: int) -> ProcessOutcome:
        """``paper-trail process --resume ID`` (slice C)."""
        return self.process.resume(receipt_id)

    def line(self, receipt_id: int, position: int) -> LineItem:
        return self.receipt(receipt_id).items[position]

    def match_sources(self, receipt_id: int) -> list[MatchSource | None]:
        return [item.match_source for item in self.receipt(receipt_id).items]

    def product(self, product_id: int) -> Product:
        with self.uow_factory(read_only=True) as uow:
            found = uow.products.get(product_id)
        assert found is not None, f"product {product_id} is not stored"
        return found

    def product_ids(self) -> list[int]:
        """Every stored product's id. Products are numbered from 1 and never deleted here."""
        found: list[int] = []
        with self.uow_factory(read_only=True) as uow:
            while uow.products.get(len(found) + 1) is not None:
                found.append(len(found) + 1)
        return found

    def aliases(self, product_id: int) -> tuple[ProductAlias, ...]:
        with self.uow_factory(read_only=True) as uow:
            return uow.products.aliases(product_id)

    def find_by_alias(self, alias: str, store_key: str | None) -> Product | None:
        with self.uow_factory(read_only=True) as uow:
            return uow.products.find_by_alias(alias, store_key)

    def price_history(self, product_id: int) -> tuple[PriceObservation, ...]:
        with self.uow_factory(read_only=True) as uow:
            return uow.prices.history(product_id)

    def receipt(self, receipt_id: int) -> Receipt:
        with self.uow_factory(read_only=True) as uow:
            found = uow.receipts.get(receipt_id)
        assert found is not None, f"receipt {receipt_id} is not stored"
        return found

    def review(self, review_id: int) -> ReviewItem:
        with self.uow_factory(read_only=True) as uow:
            found = uow.reviews.get(review_id)
        assert found is not None, f"review {review_id} is not stored"
        return found

    def open_reviews(self) -> tuple[ReviewItem, ...]:
        with self.uow_factory(read_only=True) as uow:
            return uow.reviews.list_open(PageRequest(limit=100)).items

    def latest_correction(
        self,
        subject: str,
        kind: CorrectionKind = CorrectionKind.FIELD,
        store_key: str | None = None,
    ) -> Correction | None:
        with self.uow_factory(read_only=True) as uow:
            return uow.corrections.latest(kind, subject, store_key)

    def events(self, receipt_id: int) -> tuple[TraceEvent, ...]:
        return self.tracer.events(receipt_id)

    def steps(self, receipt_id: int) -> list[tuple[TraceKind, str]]:
        """(kind, name) of every event but NODE start/end: the rows of the terminal trace."""
        return [
            (event.kind, event.name)
            for event in self.events(receipt_id)
            if event.kind is not TraceKind.NODE
        ]

    def node_runs(self, receipt_id: int) -> list[str]:
        """The names of the nodes that ran, in order (one per NODE start event)."""
        return [
            event.name
            for event in self.events(receipt_id)
            if event.kind is TraceKind.NODE and event.data.get("phase") == "start"
        ]


def build_harness(  # noqa: PLR0913 - a test builder: keyword-only overrides with defaults
    reader: ReceiptReader,
    *,
    settings: Settings | None = None,
    clock: FakeClock | None = None,
    uow_factory: UnitOfWorkFactory | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    images: FakeImageStore | None = None,
    wrap_tools: Callable[[ToolGateway], ToolGateway] | None = None,
    catalog: ProductCatalog | None = None,
    lookups: Sequence[ProductLookup] = (),
    guesser: ReceiptReader | None = None,
) -> Harness:
    """The graph over fakes and a new SQLite database (``scratch_database``), Claude being
    ``reader`` (a ``ScriptedReader``, or one wrapped in ``BudgetedReader``). Pass the same
    ``uow_factory``, ``checkpointer`` and ``images`` to a second harness to stand for a second
    process on the same data (resume, review).
    ``catalog`` is the reference catalog asked after the store catalog (``product_codes`` of
    ``uow_factory``), as ``bootstrap`` puts the PLU table there; an empty ``FakeCatalog`` by
    default. ``lookups`` (fakes) and ``guesser`` (a second ``ScriptedReader``) turn on the
    lookups and the guess after the catalog entries; with neither, there is no enricher."""
    clock = clock or FakeClock(NOW)
    settings = settings or agent_settings()
    uow_factory = uow_factory or _scratch_uow_factory(clock)
    images = images or FakeImageStore()
    tracer = FakeTracer(clock)
    tolerances = Tolerances()
    record = RecordReceipt(uow_factory, clock, tolerances)
    composite = CompositeCatalog(StoreCatalog(uow_factory), catalog or FakeCatalog())
    registry = build_tool_registry(
        ToolServices(
            uow_factory=uow_factory,
            record_receipt=record,
            tolerances=tolerances,
            catalog=composite,
        )
    )
    enricher = (
        ProductEnricher(
            uow_factory,
            composite,
            lookups,
            reader=guesser,
            model=EXTRACT_MODEL,
            clock=clock,
            throttle=Throttle(monotonic=lambda: 0.0, sleep=lambda _: None),
        )
        if lookups or guesser is not None
        else None
    )
    tools = _TRANSPORT.gateway(registry)
    if wrap_tools is not None:
        tools = wrap_tools(tools)
    deps = AgentDeps(
        tools=tools,
        tracer=tracer,
        clock=clock,
        settings=settings,
        prompts=PromptLibrary(),
        images=images,
        reader=reader,
        enricher=enricher,
    )
    checkpointer = checkpointer or InMemorySaver()
    runner = LangGraphReceiptRunner(deps, checkpointer)
    return Harness(
        clock=clock,
        uow_factory=uow_factory,
        images=images,
        reader=reader,
        tracer=tracer,
        settings=settings,
        registry=registry,
        deps=deps,
        checkpointer=checkpointer,
        runner=runner,
        process=ProcessReceiptFile(
            images, uow_factory, runner, tracer, clock, hold=receipt_hold(uow_factory)
        ),
        resolve=ResolveReview(uow_factory, runner, record, clock, hold=receipt_hold(uow_factory)),
        record=record,
        correct=CorrectLineItem(uow_factory, clock),
    )
