"""``paper-trail``: serve the API, run the worker, migrate the database, import and show
receipts, process receipts through the agents and resolve their reviews, call the agents' tools
or serve them over MCP, export the API spec.

This is the composition boundary of every process: the only module that reads the
environment (through ``Settings``) and configures logging. Commands whose stdout is their
answer (`openapi`, `db current`, `receipts show`, `graph`, `version`) leave logging
unconfigured; the agent commands (`process`, `review`, `tools`) print their answer on stdout and
log to stderr, at WARNING unless DEBUG is asked for (the trace files are the full record).
`serve`, `worker` and `mcp` log JSON lines on stdout.

Start-up time: a command imports what only it needs (the web app and uvicorn, the agent
commands and with them LangGraph, the MCP SDK and the model adapters) inside its own body, so
``db upgrade``, ``worker`` or ``--help`` do not load them (``tests/e2e/test_cli_startup.py``).
"""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import structlog
import typer
from pydantic import ValidationError

import paper_trail
from paper_trail.bootstrap import (
    Container,
    build_container,
    database_revision,
    dispose,
    upgrade_database,
)
from paper_trail.config import Settings, env_var_name
from paper_trail.domain.enums import AgentRole, Category
from paper_trail.interfaces.cli.options import Transport, with_transport
from paper_trail.interfaces.cli.receipts import (
    EXIT_INVALID_INPUT,
    fail,
    import_receipt,
    show_receipt,
)
from paper_trail.interfaces.worker.heartbeat import HEARTBEAT_FILENAME, HeartbeatFile
from paper_trail.interfaces.worker.runner import WorkerRunner
from paper_trail.observability import configure_logging

STALE_AFTER_HEARTBEATS = 3

app = typer.Typer(
    name="paper-trail",
    help="Paper Trail: receipts in, searchable purchases and price history out.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
db_app = typer.Typer(help="Database schema migrations.", no_args_is_help=True)
app.add_typer(db_app, name="db")
receipts_app = typer.Typer(help="Import and inspect receipts.", no_args_is_help=True)
app.add_typer(receipts_app, name="receipts")
review_app = typer.Typer(
    help="Receipts parked for a person: list, show, resolve.", no_args_is_help=True
)
app.add_typer(review_app, name="review")
tools_app = typer.Typer(
    help="The agents' tools: list them per role, call one as a role.", no_args_is_help=True
)
app.add_typer(tools_app, name="tools")
items_app = typer.Typer(
    help="Receipt lines: correct the product a line is linked to.", no_args_is_help=True
)
app.add_typer(items_app, name="items")

RunOption = Annotated[
    str | None,
    typer.Option(
        "--run",
        metavar="NAME",
        help="An isolated run: its own database and traces under DATA_DIR/runs/NAME.",
    ),
]

TransportOption = Annotated[
    Transport | None,
    typer.Option(
        "--transport",
        help="How tools are called (default: PAPER_TRAIL_TOOLS__TRANSPORT).",
        show_default=False,
    ),
]

log = structlog.get_logger(__name__)


def load_settings() -> Settings:
    """Read Settings from the environment; on invalid values exit with one line per run."""
    try:
        return Settings()
    except ValidationError as error:
        problems = "; ".join(
            f"{env_var_name(detail['loc'])}: {detail['msg']}" if detail["loc"] else detail["msg"]
            for detail in error.errors()
        )
        typer.echo(f"paper-trail: invalid configuration: {problems}", err=True)
        raise typer.Exit(os.EX_CONFIG) from None


def configure_agent_logging(settings: Settings) -> None:
    """Logs of the agent commands go to stderr, quiet unless DEBUG is asked for."""
    level = "DEBUG" if settings.log_level == "DEBUG" else "WARNING"
    if settings.log_level == "ERROR":
        level = "ERROR"
    configure_logging(settings.model_copy(update={"log_level": level}), to_stderr=True)


@contextmanager
def running_container(settings: Settings) -> Iterator[Container]:
    container = build_container(settings)
    try:
        yield container
    finally:
        dispose(container)


def _heartbeat(container: Container) -> HeartbeatFile:
    return HeartbeatFile(container.settings.data_dir / HEARTBEAT_FILENAME, container.clock)


@app.command()
def serve() -> None:
    """Serve the HTTP API and the web app."""
    import uvicorn

    from paper_trail.interfaces.web.app import create_app

    settings = load_settings()
    configure_logging(settings)
    if not settings.static_dir.is_dir():
        log.warning("web.api_only", static_dir=str(settings.static_dir))
    with running_container(settings) as container:
        uvicorn.run(
            create_app(container),
            host=settings.host,
            port=settings.http_port,
            proxy_headers=False,
            access_log=False,
            log_config=None,
        )


@app.command()
def worker(
    check_heartbeat: Annotated[
        bool,
        typer.Option(
            "--check-heartbeat",
            help="Exit 0 if a running worker's heartbeat is fresh, else 1 (container health).",
        ),
    ] = False,
) -> None:
    """Run the background worker until SIGTERM or SIGINT."""
    settings = load_settings()
    if check_heartbeat:
        max_age = timedelta(seconds=STALE_AFTER_HEARTBEATS * settings.worker.heartbeat_interval_s)
        with running_container(settings) as container:
            fresh = _heartbeat(container).is_fresh(max_age)
        raise typer.Exit(0 if fresh else 1)

    configure_logging(settings)
    with running_container(settings) as container:
        runner = WorkerRunner(
            handlers=(),
            clock=container.clock,
            heartbeat=_heartbeat(container),
            poll_interval_s=settings.worker.poll_interval_s,
            heartbeat_interval_s=settings.worker.heartbeat_interval_s,
            shutdown_grace_s=settings.worker.shutdown_grace_s,
        )
        exit_code = runner.run()
    raise typer.Exit(exit_code)


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Migrate the database to the newest schema. Safe to run repeatedly."""
    settings = load_settings()
    configure_logging(settings)
    with running_container(settings) as container:
        upgrade_database(container)
        log.info("db.upgraded", revision=database_revision(container))


@db_app.command("current")
def db_current() -> None:
    """Print the schema revision the database is at."""
    with running_container(load_settings()) as container:
        typer.echo(database_revision(container) or "none")


@receipts_app.command("import")
def receipts_import(
    file: Annotated[
        Path,
        typer.Argument(
            metavar="FILE",
            exists=True,
            dir_okay=False,
            readable=True,
            help="A receipt as JSON (see README).",
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate and check the arithmetic; write nothing."),
    ] = False,
) -> None:
    """Record a receipt from a JSON file. Exits 0 when recorded, even if it needs review."""
    settings = load_settings()
    configure_logging(settings)
    with running_container(settings) as container:
        import_receipt(container, file, dry_run=dry_run)


@receipts_app.command("show")
def receipts_show(receipt_id: Annotated[int, typer.Argument(metavar="ID")]) -> None:
    """Print a receipt as JSON, in the shape of GET /api/receipts/{id}."""
    with running_container(load_settings()) as container:
        show_receipt(container, receipt_id)


@app.command()
def process(  # noqa: PLR0913 - one parameter per command-line option
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            metavar="PATH...",
            help="Receipt images, or folders of them (not recursed).",
            show_default=False,
        ),
    ] = None,
    *,
    run: RunOption = None,
    max_text_attempts: Annotated[
        int | None,
        typer.Option("--max-text-attempts", min=1, help="Extraction attempts on the OCR text."),
    ] = None,
    no_feedback: Annotated[
        bool, typer.Option("--no-feedback", help="Retry with a generic line, not the issues.")
    ] = False,
    no_vision: Annotated[
        bool, typer.Option("--no-vision", help="No vision re-read (max vision attempts 0).")
    ] = False,
    no_examples: Annotated[
        bool, typer.Option("--no-examples", help="Leave the few-shot examples out of prompts.")
    ] = False,
    prompt_version: Annotated[
        str | None,
        typer.Option("--prompt-version", metavar="V", help="Extraction prompt version, e.g. v1."),
    ] = None,
    transport: TransportOption = None,
    text_file: Annotated[
        Path | None,
        typer.Option(
            "--text-file", metavar="FILE", help="Process OCR text from a file instead of images."
        ),
    ] = None,
    interrupt_after: Annotated[
        str | None,
        typer.Option(
            "--interrupt-after",
            metavar="NODE",
            help="Pause each receipt after this node of the graph (its checkpoint is kept).",
        ),
    ] = None,
    resume: Annotated[
        int | None,
        typer.Option(
            "--resume",
            metavar="ID",
            help="Continue a paused or stopped receipt from its last checkpoint (no PATH).",
        ),
    ] = None,
    reprocess: Annotated[
        bool,
        typer.Option("--reprocess", help="Run already stored photos again (from their OCR text)."),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="Also print the feedback and the model's answers.")
    ] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Plain text output.")] = False,
) -> None:
    """Process receipts through the agents and print a trace per receipt."""
    from paper_trail.interfaces.cli.process import (
        ProcessOptions,
        process_receipts,
        resume_receipt,
    )

    settings = load_settings()
    configure_agent_logging(settings)
    options = ProcessOptions(
        run=run,
        max_text_attempts=max_text_attempts,
        feedback=not no_feedback,
        vision=not no_vision,
        examples=not no_examples,
        prompt_version=prompt_version,
        transport=transport,
        interrupt_after=interrupt_after,
        reprocess=reprocess,
        verbose=verbose,
        color=not no_color,
    )
    if resume is None:
        process_receipts(settings, paths or [], text_file, options)
        return
    if paths or text_file is not None or reprocess or interrupt_after is not None:
        fail(
            EXIT_INVALID_INPUT,
            "--resume ID continues a stored receipt: it takes no PATH, --text-file,"
            " --reprocess or --interrupt-after",
        )
    # A resumed thread keeps the configuration it started with (slice C review note 5).
    changing = {
        "--max-text-attempts": max_text_attempts is not None,
        "--no-feedback": no_feedback,
        "--no-vision": no_vision,
        "--no-examples": no_examples,
        "--prompt-version": prompt_version is not None,
    }
    if refused := [flag for flag, given in changing.items() if given]:
        fail(
            EXIT_INVALID_INPUT,
            f"--resume ID continues a run with the configuration it started with: it takes no"
            f" {', '.join(refused)}",
        )
    resume_receipt(settings, resume, options)


@review_app.command("list")
def review_list(run: RunOption = None) -> None:
    """List the open reviews."""
    from paper_trail.interfaces.cli.review import list_reviews

    settings = load_settings()
    configure_agent_logging(settings)
    list_reviews(settings, run)


@review_app.command("show")
def review_show(
    review_id: Annotated[int, typer.Argument(metavar="ID")], run: RunOption = None
) -> None:
    """Show a review: its reason, issues and the extraction waiting for a decision."""
    from paper_trail.interfaces.cli.review import show_review

    settings = load_settings()
    configure_agent_logging(settings)
    show_review(settings, run, review_id)


@review_app.command("resolve")
def review_resolve(  # noqa: PLR0913 - one parameter per command-line option
    review_id: Annotated[int, typer.Argument(metavar="ID")],
    *,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            metavar="F",
            exists=True,
            dir_okay=False,
            readable=True,
            help="The corrected receipt as JSON (the `receipts import` format).",
        ),
    ] = None,
    accept: Annotated[
        bool, typer.Option("--accept", help="Accept the extraction the review holds.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Keep a receipt that does not add up (needs --reason).")
    ] = False,
    reason: Annotated[
        str | None, typer.Option("--reason", metavar="TEXT", help="Why it is forced.")
    ] = None,
    run: RunOption = None,
) -> None:
    """Resolve a review: resume the receipt's graph with a corrected or accepted draft."""
    from paper_trail.interfaces.cli.review import resolve_review

    settings = load_settings()
    configure_agent_logging(settings)
    resolve_review(settings, run, review_id, file, accept=accept, force=force, reason=reason)


@tools_app.command("list")
def tools_list(
    role: Annotated[
        AgentRole | None,
        typer.Option("--role", metavar="ROLE", help="Only this role's tools.", show_default=False),
    ] = None,
    transport: TransportOption = None,
) -> None:
    """List the tools each role may call. Needs neither a database nor the LLM host."""
    from paper_trail.interfaces.cli.tools import list_tools

    settings = with_transport(load_settings(), transport)
    configure_agent_logging(settings)
    list_tools(settings, role)


@tools_app.command("call")
def tools_call(
    tool: Annotated[str, typer.Argument(metavar="TOOL")],
    payload: Annotated[str, typer.Argument(metavar="JSON", help="The input, a JSON object.")],
    *,
    role: Annotated[
        AgentRole, typer.Option("--role", metavar="ROLE", help="Call the tool as this role.")
    ],
    transport: TransportOption = None,
) -> None:
    """Call a tool as a role and print its result as JSON. Exits 5 when the role may not."""
    from paper_trail.interfaces.cli.tools import call_tool

    settings = with_transport(load_settings(), transport)
    configure_agent_logging(settings)
    call_tool(settings, role, tool, payload)


@items_app.command("correct")
def items_correct(  # noqa: PLR0913 - one parameter per command-line option
    receipt_id: Annotated[int, typer.Argument(metavar="RECEIPT_ID")],
    position: Annotated[
        int, typer.Argument(metavar="POSITION", help="The line's 0-based position, as stored.")
    ],
    *,
    product_id: Annotated[
        int | None,
        typer.Option("--product-id", metavar="N", help="Link the line to this product."),
    ] = None,
    new_product: Annotated[
        str | None,
        typer.Option("--new-product", metavar="NAME", help="Link it to a new product."),
    ] = None,
    category: Annotated[
        Category | None,
        typer.Option("--category", metavar="C", help="Remember this category for the line."),
    ] = None,
    run: RunOption = None,
) -> None:
    """Correct a line's product; matching remembers it for that text at that store."""
    from paper_trail.interfaces.cli.items import correct_item

    settings = load_settings()
    configure_agent_logging(settings)
    correct_item(
        settings,
        run,
        receipt_id,
        position,
        product_id=product_id,
        new_product=new_product,
        category=category,
    )


@app.command("mcp")
def mcp_server() -> None:
    """Serve the agents' tools over MCP, one endpoint per role at /mcp/<role>/."""
    from paper_trail.interfaces.cli.tools import serve_mcp

    settings = load_settings()
    configure_logging(settings)
    serve_mcp(settings)


@app.command()
def graph(
    mermaid: Annotated[
        bool, typer.Option("--mermaid", help="Print the graph as a Mermaid diagram.")
    ] = False,
) -> None:
    """Print the agents' graph. Needs neither a database nor the LLM host."""
    from paper_trail.bootstrap.agents import receipt_graph_mermaid

    if not mermaid:
        typer.echo("paper-trail: choose an output format: --mermaid", err=True)
        raise typer.Exit(2)
    typer.echo(receipt_graph_mermaid(load_settings()), nl=False)


@app.command()
def openapi(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write to this file instead of stdout."),
    ] = None,
) -> None:
    """Print the OpenAPI document the TypeScript client is generated from."""
    from paper_trail.interfaces.web.app import create_app

    with running_container(load_settings()) as container:
        spec = create_app(container).openapi()
    document = json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if output is None:
        typer.echo(document, nl=False)
    else:
        output.write_text(document, encoding="utf-8")


@app.command()
def version() -> None:
    """Print the version and the build's git SHA."""
    settings = load_settings()
    typer.echo(f"paper-trail {paper_trail.__version__} ({settings.build_sha or 'unknown'})")
