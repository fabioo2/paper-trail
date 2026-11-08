"""Composition root of the agent core (agent-core plan 2, 4): the graph, its tools, the LLM.

Only the agent commands (``process``, ``review``, ``items``, ``tools``, ``mcp``, ``graph``)
import this module, so LangGraph, the MCP SDK and the model adapters load only for them.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass

from langgraph.checkpoint.memory import InMemorySaver

from paper_trail.adapters.filesystem.image_store import FilesystemImageStore
from paper_trail.adapters.mcp.gateway import McpToolGateway
from paper_trail.adapters.ollama.embeddings import OllamaEmbeddings
from paper_trail.adapters.ollama.llm import OllamaLlm
from paper_trail.adapters.sqlite.checkpoints import sqlite_checkpointer
from paper_trail.adapters.tesseract.ocr import TesseractOcr
from paper_trail.adapters.trace.json_file import JsonTraceWriter
from paper_trail.adapters.trace.memory import InMemoryTracer
from paper_trail.agents.graph import AgentDeps, build_receipt_graph
from paper_trail.agents.nodes.supervisor import graph_limits
from paper_trail.agents.prompts import PromptLibrary
from paper_trail.agents.runner import LangGraphReceiptRunner
from paper_trail.application.process_receipt import ProcessReceiptFile
from paper_trail.application.receipts import GetReview, ListOpenReviews, ResolveReview
from paper_trail.bootstrap import (
    IMAGES_DIRNAME,
    RUNS_DIRNAME,
    Container,
    build_container,
    dispose,
)
from paper_trail.config import McpSettings, Settings
from paper_trail.ports.agents import ReceiptGraphRunner
from paper_trail.ports.clock import Clock
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
    limits = graph_limits(settings)
    return RunConfig(
        models={
            "text": settings.ollama.text_model,
            "vision": settings.ollama.vision_model,
            "embed": settings.ollama.embed_model,
        },
        prompt_versions={
            "extraction": settings.prompts.extraction_version,
            "matching": settings.prompts.matching_version,
            "enrichment": settings.prompts.enrichment_version,
        },
        examples=settings.prompts.examples,
        feedback=limits.feedback,
        max_text_attempts=limits.max_text_attempts,
        max_vision_attempts=limits.max_vision_attempts,
        enrichment=settings.graph.enrichment,
        transport=settings.tools.transport,
    )


def _image_store(settings: Settings) -> FilesystemImageStore:
    return FilesystemImageStore(
        settings.data_dir / IMAGES_DIRNAME, max_pixels=settings.upload.max_pixels
    )


def tool_registry(container: Container) -> ToolRegistry:
    """Every tool, over the container's database and the shared image store."""
    settings = container.settings
    return build_tool_registry(
        ToolServices(
            ocr=TesseractOcr(settings.ocr),
            image_store=_image_store(settings),
            uow_factory=container.uow_factory,
            record_receipt=container.record_receipt,
            tolerances=container.tolerances,
            embeddings=OllamaEmbeddings(settings.ollama),
        )
    )


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


def _agent_deps(settings: Settings, clock: Clock, tracer: Tracer, tools: ToolGateway) -> AgentDeps:
    return AgentDeps(
        llm=OllamaLlm(settings.ollama),
        tools=tools,
        tracer=tracer,
        clock=clock,
        settings=settings,
        prompts=PromptLibrary(),
        images=_image_store(settings),
    )


@contextmanager
def agent_container(
    settings: Settings, run: str, *, tool_server: ToolServer | None = None
) -> Iterator[AgentContainer]:
    """Wire the agents over ``settings.database_path`` (which must be migrated) with LangGraph's
    checkpoints in the same file. Traces go to ``<DATA_DIR>/runs/<run>/``; images to
    ``<DATA_DIR>/images`` whatever the run. With ``TOOLS__TRANSPORT=mcp`` the tools are served
    by ``tool_server`` and called over MCP."""
    with ExitStack() as stack:
        container = build_container(settings)
        stack.callback(dispose, container)
        checkpointer = stack.enter_context(sqlite_checkpointer(settings.database_path))
        tools = stack.enter_context(
            _run_tool_gateway(settings, tool_registry(container), tool_server)
        )
        tracer = InMemoryTracer(container.clock)
        runner = LangGraphReceiptRunner(
            _agent_deps(settings, container.clock, tracer, tools), checkpointer
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
                _image_store(settings), uow_factory, runner, tracer, clock
            ),
            resolve_review=ResolveReview(uow_factory, runner, container.record_receipt, clock),
            list_open_reviews=ListOpenReviews(uow_factory),
            get_review=GetReview(uow_factory),
        )


def receipt_graph_mermaid(settings: Settings) -> str:
    """The compiled receipt graph as Mermaid. Opens no database and contacts no LLM host."""
    container = build_container(settings)
    try:
        tools = InProcessToolGateway(tool_registry(container))
        deps = _agent_deps(settings, container.clock, InMemoryTracer(container.clock), tools)
        return build_receipt_graph(deps, InMemorySaver()).get_graph().draw_mermaid()
    finally:
        dispose(container)
