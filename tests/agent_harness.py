"""The receipt graph wired from fakes, the way ``bootstrap`` wires it from adapters.

Every graph-level test (unit, integration, contract) builds its system under test here, so the
production constructors are named in exactly one test module:

- ``RecordReceipt(uow_factory, clock, tolerances)`` (Stage 1, gains ``replace``)
- ``build_tool_registry(ToolServices(...))`` and ``InProcessToolGateway(registry)``
- ``LangGraphReceiptRunner(AgentDeps(...), checkpointer)``: applies the tracing decorators itself;
  slice D adds ``AgentDeps.images`` (the image store the vision attempt reads the photo from)
- ``ProcessReceiptFile(image_store, uow_factory, runner, tracer, clock)``
- ``ResolveReview(uow_factory, runner, record_receipt, clock)``
- ``CorrectLineItem(uow_factory, clock)`` (slice C)

The LLM is always a ``ScriptedLlm`` and the embeddings a ``FakeEmbeddings`` (slice C: the
``product_similarity_search`` and ``save_matches`` tools embed through ``ToolServices.embeddings``);
the checkpointer is LangGraph's in-memory saver unless a test passes a ``SqliteSaver``
(integration).

The tool transport (S5-AC-04): ``build_harness`` puts ``InProcessToolGateway(registry)`` in
front of the registry, or, inside ``tool_transport("mcp")``, ``McpToolGateway(McpSettings)``
talking to the production MCP app served over that harness's registry on a loopback port
(``tests/mcp_server.py``). Graph suites run in-process; ``tests/unit/agents/
test_transport_equivalence.py`` runs representative scenarios over both and compares them.
"""

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from paper_trail.adapters.mcp.gateway import McpToolGateway
from paper_trail.agents.graph import AgentDeps
from paper_trail.agents.prompts import PromptLibrary
from paper_trail.agents.runner import LangGraphReceiptRunner
from paper_trail.application.corrections import CorrectLineItem
from paper_trail.application.process_receipt import (
    ProcessOutcome,
    ProcessReceiptFile,
    ReceiptSource,
)
from paper_trail.application.receipts import RecordReceipt, ResolveReview
from paper_trail.config import (
    McpSettings,
    OllamaSettings,
    PromptSettings,
    Settings,
)
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
from paper_trail.ports.tools import ToolGateway
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from paper_trail.tools.handlers import ToolServices
from paper_trail.tools.inprocess import InProcessToolGateway
from paper_trail.tools.registry import ToolRegistry, build_tool_registry
from tests.extractions import COSTCO_OCR, NOW
from tests.fakes.clock import FakeClock
from tests.fakes.embeddings import FakeEmbeddings
from tests.fakes.images import FakeImageStore, fake_png
from tests.fakes.llm import ScriptedLlm
from tests.fakes.ocr import FakeOcr
from tests.fakes.trace import FakeTracer
from tests.fakes.unit_of_work import FakeDatabase, fake_uow_factory
from tests.mcp_server import serve_mcp

TEXT_MODEL = "text-model:test"
# GRAPH__VISION_FIRST_BELOW until 2026-11-08 (production now sends every photo vision first):
# the graph suites of the text-first fallback keep it, so FakeOcr's 0.87 reads text first.
TEXT_FIRST_BELOW = 0.65
VISION_MODEL = "vision-model:test"
TOOL_TRANSPORTS = ("inprocess", "mcp")
MCP_TIMEOUT_S = 10.0

type GatewayFactory = Callable[[ToolRegistry], ToolGateway]


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


def agent_settings(  # noqa: PLR0913 - a test builder: keyword-only overrides with defaults
    *,
    max_text_attempts: int = 2,
    max_vision_attempts: int = 0,
    min_confidence: float = 0.6,
    feedback: bool = True,
    examples: bool = True,
    auto_link_max_distance: float = 0.15,
    review_max_distance: float = 0.30,
    enrichment: bool = False,
    extraction_version: str = "v1",
    vision_first_below: float | None = TEXT_FIRST_BELOW,
) -> Settings:
    """Settings for graph tests. The slice D stages are off unless a test turns them on
    (``max_vision_attempts=1`` or 2, and ``enrichment=True``), so the slice A-C suites keep
    pinning exactly the calls and events of the stages they are about. The matching thresholds
    are PLAN 1.3's defaults (``MATCHING__AUTO_LINK_MAX_DISTANCE``,
    ``MATCHING__REVIEW_MAX_DISTANCE``).

    The text extraction prompt stays ``extraction/v1`` (integer cents) unless a test asks for
    ``extraction_version="v2"`` (the printed-value answer, docs/plans/extraction-accuracy.md),
    so the suites written before it keep scripting ``extraction()``; the vision attempt always
    uses ``vision/v1`` and is scripted with ``printed_extraction()``. ``vision_first_below``
    is ``GRAPH__VISION_FIRST_BELOW``: ``TEXT_FIRST_BELOW`` (0.65, the default until
    2026-11-08) unless a test passes another value, or None for the production default (1.0:
    every photo vision first). ``FakeOcr``'s confidence (0.87) is above 0.65, so in the suites
    written for the text-first fallback a photo still starts with the text attempts."""
    graph: dict[str, object] = {
        "max_text_attempts": max_text_attempts,
        "max_vision_attempts": max_vision_attempts,
        "min_confidence": min_confidence,
        "feedback": feedback,
        "enrichment": enrichment,
    }
    if vision_first_below is not None:
        graph["vision_first_below"] = vision_first_below
    return Settings.model_validate(
        {
            "env": "test",
            "graph": graph,
            "prompts": PromptSettings(examples=examples, extraction_version=extraction_version),
            "ollama": OllamaSettings(text_model=TEXT_MODEL, vision_model=VISION_MODEL),
            "matching": {
                "auto_link_max_distance": auto_link_max_distance,
                "review_max_distance": review_max_distance,
            },
        }
    )


@dataclass
class Harness:
    clock: FakeClock
    uow_factory: UnitOfWorkFactory
    images: FakeImageStore
    ocr: FakeOcr
    llm: ScriptedLlm
    embeddings: FakeEmbeddings
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

    def process_text(
        self, text: str, name: str = "receipt.txt", *, interrupt_after: str | None = None
    ) -> ProcessOutcome:
        return self.process(ReceiptSource(name=name, text=text), interrupt_after=interrupt_after)

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

    def has_vector(self, product_id: int) -> bool:
        with self.uow_factory(read_only=True) as uow:
            vectors = uow.vectors
            probe = [0.0] * vectors.dimension
            probe[0] = 1.0
            return any(hit.product_id == product_id for hit in vectors.nearest(probe, k=10_000))

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
    llm: ScriptedLlm,
    *,
    settings: Settings | None = None,
    ocr_text: str = COSTCO_OCR,
    clock: FakeClock | None = None,
    uow_factory: UnitOfWorkFactory | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    images: FakeImageStore | None = None,
    wrap_tools: Callable[[ToolGateway], ToolGateway] | None = None,
    embeddings: FakeEmbeddings | None = None,
) -> Harness:
    """The graph over fakes. Pass the same ``uow_factory``, ``checkpointer`` and ``images`` to a
    second harness to stand for a second process on the same data (resume, review)."""
    clock = clock or FakeClock(NOW)
    settings = settings or agent_settings()
    uow_factory = uow_factory or fake_uow_factory(FakeDatabase(), clock)
    images = images or FakeImageStore()
    embeddings = embeddings or FakeEmbeddings()
    ocr = FakeOcr(ocr_text)
    tracer = FakeTracer(clock)
    tolerances = Tolerances()
    record = RecordReceipt(uow_factory, clock, tolerances)
    registry = build_tool_registry(
        ToolServices(
            ocr=ocr,
            image_store=images,
            uow_factory=uow_factory,
            record_receipt=record,
            tolerances=tolerances,
            embeddings=embeddings,
        )
    )
    tools = _TRANSPORT.gateway(registry)
    if wrap_tools is not None:
        tools = wrap_tools(tools)
    deps = AgentDeps(
        llm=llm,
        tools=tools,
        tracer=tracer,
        clock=clock,
        settings=settings,
        prompts=PromptLibrary(),
        images=images,
    )
    checkpointer = checkpointer or InMemorySaver()
    runner = LangGraphReceiptRunner(deps, checkpointer)
    return Harness(
        clock=clock,
        uow_factory=uow_factory,
        images=images,
        ocr=ocr,
        llm=llm,
        embeddings=embeddings,
        tracer=tracer,
        settings=settings,
        registry=registry,
        deps=deps,
        checkpointer=checkpointer,
        runner=runner,
        process=ProcessReceiptFile(images, uow_factory, runner, tracer, clock),
        resolve=ResolveReview(uow_factory, runner, record, clock),
        record=record,
        correct=CorrectLineItem(uow_factory, clock),
    )
