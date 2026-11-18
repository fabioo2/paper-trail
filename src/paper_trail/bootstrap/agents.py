"""Composition root of the agent core (agent-core plan 2, 4): the graph, its tools, Claude.

Only the agent commands (``process``, ``review``, ``items``, ``tools``, ``mcp``, ``graph``)
import this module, so LangGraph, the MCP SDK and the Anthropic SDK load only for them.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import Engine

from paper_trail.adapters.anthropic.reader import AnthropicReader
from paper_trail.adapters.catalog.composite import CompositeCatalog
from paper_trail.adapters.catalog.plu import PluCatalog
from paper_trail.adapters.lookups.http import (
    OPEN_BEAUTY_FACTS,
    OPEN_FOOD_FACTS,
    OPEN_PRODUCTS_FACTS,
    lookup_client,
)
from paper_trail.adapters.lookups.openfacts import OpenFactsLookup
from paper_trail.adapters.lookups.upcitemdb import UpcItemDbLookup
from paper_trail.adapters.mcp.gateway import McpToolGateway
from paper_trail.adapters.sqlite.checkpoints import sqlite_checkpointer
from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.spend_ledger import SqliteSpendLedger
from paper_trail.adapters.sqlite.store_catalog import StoreCatalog
from paper_trail.adapters.trace.json_file import JsonTraceWriter
from paper_trail.adapters.trace.memory import InMemoryTracer
from paper_trail.agents.graph import AgentDeps, build_receipt_graph
from paper_trail.agents.prompts import PromptLibrary
from paper_trail.agents.runner import LangGraphReceiptRunner
from paper_trail.application.budget import BudgetedReader
from paper_trail.application.enrichment import EnrichCatalog, ProductEnricher
from paper_trail.application.process_receipt import ProcessReceiptFile
from paper_trail.application.receipts import (
    AnswerReview,
    GetPendingReview,
    GetReview,
    ListOpenReviews,
    ListPendingReviews,
    ResolveReview,
)
from paper_trail.bootstrap import (
    RUNS_DIRNAME,
    Container,
    ReviewServices,
    build_container,
    dispose,
    image_store,
)
from paper_trail.config import McpSettings, Settings
from paper_trail.ports.agents import ReceiptGraphRunner
from paper_trail.ports.clock import Clock
from paper_trail.ports.lookups import ProductLookup
from paper_trail.ports.reader import ReceiptReader
from paper_trail.ports.tools import ToolGateway
from paper_trail.ports.trace import RunConfig, Tracer, TraceWriter
from paper_trail.tools.handlers import ToolServices
from paper_trail.tools.inprocess import InProcessToolGateway
from paper_trail.tools.registry import ToolRegistry, build_tool_registry

# Serves a tool registry over MCP for the duration of the block and yields its base URL
# (``interfaces.mcp.server.serve_on_loopback``); the entry points pass it in, so the
# composition root does not depend on them.
type ToolServer = Callable[[ToolRegistry], AbstractContextManager[str]]


@dataclass(frozen=True, slots=True)
class AgentContainer:
    """What ``paper-trail process`` and ``paper-trail review`` work with, for one named run."""

    container: Container
    run: str
    run_config: RunConfig
    tracer: Tracer
    trace_writer: TraceWriter
    runner: ReceiptGraphRunner
    process_receipt: ProcessReceiptFile
    resolve_review: ResolveReview
    list_open_reviews: ListOpenReviews
    get_review: GetReview


def run_config(settings: Settings) -> RunConfig:
    """What a trace records about the configuration of a run: the values the graph runs with."""
    return RunConfig(
        models={"extract": settings.claude.extract_model, "retry": settings.claude.retry_model},
        prompt_versions={"extraction": settings.prompts.extraction_version},
        feedback=settings.graph.feedback,
        max_attempts=settings.graph.max_attempts,
        enrichment=settings.graph.enrichment,
        catalog=settings.catalog.enabled,
        transport=settings.tools.transport,
    )


def tool_registry(container: Container) -> ToolRegistry:
    """Every tool, over the container's database."""
    return build_tool_registry(
        ToolServices(
            uow_factory=container.uow_factory,
            record_receipt=container.record_receipt,
            tolerances=container.tolerances,
            catalog=product_catalog(container),
        )
    )


def product_catalog(container: Container) -> CompositeCatalog:
    """The store catalog (the container's ``product_codes``), then the PLU table."""
    plu_path = container.settings.catalog.plu_path
    if plu_path is None:
        msg = "Settings fills in CATALOG__PLU_PATH"
        raise ValueError(msg)
    return CompositeCatalog(StoreCatalog(container.uow_factory), PluCatalog(plu_path))


@contextmanager
def _run_tool_gateway(
    settings: Settings, registry: ToolRegistry, tool_server: ToolServer | None
) -> Iterator[ToolGateway]:
    """The gateway a run's agents call: the registry itself, or the registry served over MCP
    by ``tool_server`` in this process (a standalone server would not see the run's database)."""
    if settings.tools.transport == "inprocess":
        yield InProcessToolGateway(registry)
        return
    if tool_server is None:
        msg = "the mcp tool transport needs a tool server"
        raise ValueError(msg)
    with tool_server(registry) as url:
        yield McpToolGateway(
            McpSettings.model_validate({"url": url, "timeout_s": settings.mcp.timeout_s})
        )


@contextmanager
def tools_gateway(settings: Settings) -> Iterator[ToolGateway]:
    """The gateway of ``paper-trail tools``: the registry over the main database, or the
    standalone MCP server at ``MCP__URL``."""
    if settings.tools.transport == "mcp":
        yield McpToolGateway(settings.mcp)
        return
    container = build_container(settings)
    try:
        yield InProcessToolGateway(tool_registry(container))
    finally:
        dispose(container)


@dataclass(frozen=True, slots=True)
class _ClaudeReader:
    """Every Claude read is checked against the month's budget and its cost recorded in the
    ledger (``BudgetedReader``); the runner adds the per-receipt tracing (review I10)."""

    api: AnthropicReader
    reader: BudgetedReader

    def check(self) -> None:
        """No read could be made now: no key, or the budget reached (no call is made)."""
        self.api.check()
        self.reader.check()


def _claude_reader(settings: Settings, clock: Clock, ledger_engine: Engine) -> _ClaudeReader:
    api = AnthropicReader(settings.claude)
    ledger = SqliteSpendLedger(ledger_engine)
    return _ClaudeReader(
        api, BudgetedReader(api, ledger, clock, settings.claude.monthly_budget_usd)
    )


def _agent_deps(
    container: Container,
    tracer: Tracer,
    tools: ToolGateway,
    reader: ReceiptReader,
    enricher: ProductEnricher | None = None,
) -> AgentDeps:
    settings = container.settings
    return AgentDeps(
        tools=tools,
        tracer=tracer,
        clock=container.clock,
        settings=settings,
        prompts=PromptLibrary(),
        images=image_store(settings),
        reader=reader,
        enricher=enricher,
    )


def _product_enricher(
    container: Container, reader: ReceiptReader, stack: ExitStack
) -> ProductEnricher:
    """Lookups and guesses over the container's database: the Open*Facts databases (unless
    ``LOOKUP__OPEN_FACTS`` is off), then UPCitemdb when ``LOOKUP__UPCITEMDB`` is on, sharing
    one HTTP client closed with ``stack``; Claude guesses with the extract model."""
    settings = container.settings
    lookups: list[ProductLookup] = []
    if settings.lookup.open_facts or settings.lookup.upcitemdb:
        client = lookup_client(settings.lookup.timeout_s)
        stack.callback(client.close)
        if settings.lookup.open_facts:
            lookups += [
                OpenFactsLookup("off", OPEN_FOOD_FACTS, client),
                OpenFactsLookup("obf", OPEN_BEAUTY_FACTS, client),
                OpenFactsLookup("opf", OPEN_PRODUCTS_FACTS, client),
            ]
        if settings.lookup.upcitemdb:
            lookups.append(UpcItemDbLookup(client))
    return ProductEnricher(
        container.uow_factory,
        product_catalog(container),
        lookups,
        reader=reader,
        model=settings.claude.extract_model,
        clock=container.clock,
    )


@contextmanager
def catalog_enrichment(container: Container, ledger_path: Path) -> Iterator[EnrichCatalog]:
    """``catalog enrich`` over the container's database; Claude's spend is kept in the main
    database at ``ledger_path``, whatever the run."""
    with ExitStack() as stack:
        ledger_engine = container.engine
        if ledger_path != container.settings.database_path:
            ledger_engine = create_engine(ledger_path)
            stack.callback(ledger_engine.dispose)
        claude = _claude_reader(container.settings, container.clock, ledger_engine)
        enricher = _product_enricher(container, claude.reader, stack)
        yield EnrichCatalog(container.uow_factory, enricher)


@contextmanager
def agent_container(
    settings: Settings,
    run: str,
    *,
    tool_server: ToolServer | None = None,
    ledger_path: Path | None = None,
) -> Iterator[AgentContainer]:
    """Wire the agents over ``settings.database_path`` (which must be migrated) with LangGraph's
    checkpoints in the same file. Traces go to ``<DATA_DIR>/runs/<run>/``; images to
    ``<DATA_DIR>/images`` whatever the run. The Claude spend is recorded in ``ledger_path``
    (the main database, migrated, whatever the run; default ``settings.database_path``), so a
    named run never starts a budget of its own. With ``TOOLS__TRANSPORT=mcp`` the tools are
    served by ``tool_server`` and called over MCP."""
    with ExitStack() as stack:
        container = build_container(settings)
        stack.callback(dispose, container)
        ledger_engine = container.engine
        if ledger_path is not None and ledger_path != settings.database_path:
            ledger_engine = create_engine(ledger_path)
            stack.callback(ledger_engine.dispose)
        claude = _claude_reader(settings, container.clock, ledger_engine)
        checkpointer = stack.enter_context(sqlite_checkpointer(settings.database_path))
        tools = stack.enter_context(
            _run_tool_gateway(settings, tool_registry(container), tool_server)
        )
        tracer = InMemoryTracer(container.clock)
        enricher = _product_enricher(container, claude.reader, stack)
        runner = LangGraphReceiptRunner(
            _agent_deps(container, tracer, tools, claude.reader, enricher), checkpointer
        )
        config = run_config(settings)
        clock, uow_factory = container.clock, container.uow_factory
        yield AgentContainer(
            container=container,
            run=run,
            run_config=config,
            tracer=tracer,
            trace_writer=JsonTraceWriter(settings.data_dir / RUNS_DIRNAME / run, run, config),
            runner=runner,
            process_receipt=ProcessReceiptFile(
                image_store(settings),
                uow_factory,
                runner,
                tracer,
                clock,
                reader_check=claude.check,
            ),
            resolve_review=ResolveReview(uow_factory, runner, container.record_receipt, clock),
            list_open_reviews=ListOpenReviews(uow_factory),
            get_review=GetReview(uow_factory),
        )


@contextmanager
def review_services(container: Container) -> Iterator[ReviewServices]:
    """The web app's reviews over the container's database and the graph's checkpoints in it.
    The web process only reads the extraction each parked thread holds; the worker resumes the
    graph with a person's answer, so the runner here gets the in-process tools and a Claude
    reader that it never calls."""
    with sqlite_checkpointer(container.settings.database_path) as checkpointer:
        tools = InProcessToolGateway(tool_registry(container))
        claude = _claude_reader(container.settings, container.clock, container.engine)
        deps = _agent_deps(container, InMemoryTracer(container.clock), tools, claude.reader)
        runner = LangGraphReceiptRunner(deps, checkpointer)
        uow_factory = container.uow_factory
        resolve = ResolveReview(uow_factory, runner, container.record_receipt, container.clock)
        yield ReviewServices(
            list_reviews=ListPendingReviews(uow_factory, runner),
            get_review=GetPendingReview(uow_factory, runner),
            answer_review=AnswerReview(uow_factory, resolve),
        )


def receipt_graph_mermaid(settings: Settings) -> str:
    """The compiled receipt graph as Mermaid. Connects to no database (the container's engine
    and the ledger's are lazy) and calls no model."""
    container = build_container(settings)
    try:
        tools = InProcessToolGateway(tool_registry(container))
        claude = _claude_reader(settings, container.clock, container.engine)
        deps = _agent_deps(container, InMemoryTracer(container.clock), tools, claude.reader)
        return build_receipt_graph(deps, InMemorySaver()).get_graph().draw_mermaid()
    finally:
        dispose(container)
