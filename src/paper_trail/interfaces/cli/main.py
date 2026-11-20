"""``paper-trail``: serve the API, run the worker, migrate the database, import and show
receipts, process receipts through the agents and resolve their reviews, correct lines, backfill
paid prices, name products by their codes, call the agents' tools or serve them over MCP, export
the API spec.

This is the composition boundary of every process: the only module that reads the
environment (through ``Settings``) and configures logging. Commands whose stdout is their
answer (`openapi`, `db current`, `receipts show`, `graph`, `version`) leave logging
unconfigured; the agent commands (`process`, `review`, `tools`) print their answer on stdout and
log to stderr, at WARNING unless DEBUG is asked for (the trace files are the full record).
`serve`, `worker` and `mcp` log JSON lines on stdout.

Start-up time: a command imports what only it needs (the web app and uvicorn, the agent
commands and with them LangGraph, the MCP SDK and the model adapters) inside its own body, so
``db upgrade``, ``worker --check-heartbeat`` or ``--help`` do not load them
(``tests/e2e/test_cli_startup.py``).
"""

import json
import os
import socket
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import structlog
import typer
from pydantic import ValidationError

import paper_trail
from paper_trail.bootstrap import (
    Container,
    backup_database,
    build_container,
    database_backup_path,
    database_is_current,
    database_revision,
    dispose,
    upgrade_database,
)
from paper_trail.config import Settings, env_var_name
from paper_trail.domain.enums import AgentRole, Category, CodeScheme
from paper_trail.interfaces.cli.options import Transport, with_transport
from paper_trail.interfaces.cli.receipts import (
    EXIT_INVALID_INPUT,
    EXIT_UNAVAILABLE,
    fail,
    import_receipt,
    show_receipt,
)
from paper_trail.interfaces.worker.backup import NightlyBackup
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
    help="Receipt lines and products: correct a line's product, merge two products.",
    no_args_is_help=True,
)
app.add_typer(items_app, name="items")
prices_app = typer.Typer(
    help="Price observations: apply discounts to receipts stored before.", no_args_is_help=True
)
app.add_typer(prices_app, name="prices")
catalog_app = typer.Typer(
    help=(
        "Products and their codes: confirm, look up or guess products, name the products"
        " receipts print codes for, fix stored codes."
    ),
    no_args_is_help=True,
)
app.add_typer(catalog_app, name="catalog")

RunOption = Annotated[
    str | None,
    typer.Option(
        "--run",
        metavar="NAME",
        help="An isolated run: its own database and traces under DATA_DIR/runs/NAME.",
    ),
]

StoreOption = Annotated[
    str | None,
    typer.Option(
        "--store", metavar="S", help="Only this store (a name or its key).", show_default=False
    ),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")]

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

    from paper_trail.bootstrap.agents import review_services
    from paper_trail.interfaces.web.app import create_app

    settings = load_settings()
    configure_logging(settings)
    if not settings.static_dir.is_dir():
        log.warning("web.api_only", static_dir=str(settings.static_dir))
    with running_container(settings) as container, ExitStack() as stack:
        reviews = None
        if database_is_current(container):
            reviews = stack.enter_context(review_services(container))
        else:
            log.warning("web.reviews_unavailable", reason="the database is not migrated")
        uvicorn.run(
            create_app(container, reviews),
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
    """Process the receipts uploaded through the API, and back the database up every night,
    until SIGTERM or SIGINT (the main database must be migrated)."""
    settings = load_settings()
    if check_heartbeat:
        max_age = timedelta(seconds=STALE_AFTER_HEARTBEATS * settings.worker.heartbeat_interval_s)
        with running_container(settings) as container:
            fresh = _heartbeat(container).is_fresh(max_age)
        raise typer.Exit(0 if fresh else 1)

    from paper_trail.application.holds import JOB_LEASE
    from paper_trail.application.jobs import ProcessReceiptJobs
    from paper_trail.interfaces.cli.runs import open_run

    configure_logging(settings)
    with open_run(settings, None) as agents:
        container = agents.container
        jobs = ProcessReceiptJobs(
            container.uow_factory,
            agents.process_receipt,
            agents.tracer,
            worker_id=f"{socket.gethostname()}-{os.getpid()}",
            lease=JOB_LEASE,
            resolve_review=agents.resolve_review,
        )
        backup = NightlyBackup(
            lambda day: backup_database(container, day),
            lambda day: database_backup_path(container, day).is_file(),
            container.clock,
            at=settings.backup.at,
        )
        runner = WorkerRunner(
            handlers=(jobs, backup),
            clock=container.clock,
            heartbeat=_heartbeat(container),
            poll_interval_s=settings.worker.poll_interval_s,
            heartbeat_interval_s=settings.worker.heartbeat_interval_s,
            shutdown_grace_s=settings.worker.shutdown_grace_s,
            ticks=(jobs.renew_lease,),
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


@db_app.command("backup")
def db_backup() -> None:
    """Back the database up now, as the worker does every night: an online, integrity-checked
    copy in <DATA_DIR>/backups/paper_trail-YYYYMMDD.db (replacing today's)."""
    settings = load_settings()
    with running_container(settings) as container:
        if not settings.database_path.is_file():
            fail(EXIT_UNAVAILABLE, f"there is no database at {settings.database_path}")
        backup = backup_database(container, container.clock.now().astimezone().date())
    typer.echo(f"{backup.path} ({backup.size_bytes:,} bytes)")
    for removed in backup.removed:
        typer.echo(f"removed {removed.name}")


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
            help="A receipt as JSON (see docs/cli.md).",
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
    max_attempts: Annotated[
        int | None,
        typer.Option(
            "--max-attempts", min=1, max=2, help="Claude reads per receipt (2: one retry)."
        ),
    ] = None,
    no_feedback: Annotated[
        bool, typer.Option("--no-feedback", help="Retry with a generic line, not the issues.")
    ] = False,
    no_catalog: Annotated[
        bool,
        typer.Option(
            "--no-catalog",
            help=(
                "Match coded lines by their text, not their code (products made so get no code:"
                " later runs with the catalog do not find them by it)."
            ),
        ),
    ] = False,
    transport: TransportOption = None,
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
        typer.Option("--reprocess", help="Read already stored photos again with Claude."),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="Also print the feedback and the model's answers.")
    ] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Plain text output.")] = False,
    table: Annotated[
        bool,
        typer.Option(
            "--table",
            help="End with a table per receipt: store, sums, models, attempts and cost.",
        ),
    ] = False,
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
        max_attempts=max_attempts,
        feedback=not no_feedback,
        catalog=not no_catalog,
        transport=transport,
        interrupt_after=interrupt_after,
        reprocess=reprocess,
        verbose=verbose,
        color=not no_color,
        table=table,
    )
    if resume is None:
        process_receipts(settings, paths or [], options)
        return
    if paths or reprocess or interrupt_after is not None:
        fail(
            EXIT_INVALID_INPUT,
            "--resume ID continues a stored receipt: it takes no PATH, --reprocess or"
            " --interrupt-after",
        )
    # A resumed thread keeps the configuration it started with (slice C review note 5).
    changing = {
        "--max-attempts": max_attempts is not None,
        "--no-feedback": no_feedback,
        "--no-catalog": no_catalog,
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
    """Resolve a review: resume the receipt's graph with a corrected draft, or accept the
    extraction it holds (the graph then saves it as it would have without a review)."""
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
    """List the tools each role may call. Needs neither a database nor Claude."""
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


@items_app.command("merge")
def items_merge(
    product_id: Annotated[int, typer.Argument(metavar="PRODUCT_ID")],
    *,
    into: Annotated[
        int,
        typer.Option("--into", metavar="N", help="The same product, which is kept."),
    ],
    apply: Annotated[
        bool, typer.Option("--apply", help="Merge (default: show what would move).")
    ] = False,
    run: RunOption = None,
) -> None:
    """Merge a product into the same one: its purchases, prices, codes and aliases move there."""
    from paper_trail.interfaces.cli.items import merge_products

    settings = load_settings()
    configure_agent_logging(settings)
    merge_products(settings, run, product_id, into, apply=apply)


@prices_app.command("backfill")
def prices_backfill(
    run: RunOption = None,
    receipt_id: Annotated[
        int | None,
        typer.Option("--receipt", metavar="ID", help="Only this receipt.", show_default=False),
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Write the links and discounts (default: dry run).")
    ] = False,
) -> None:
    """Link stored discounts to their items and record what was paid; a dry run by default."""
    from paper_trail.interfaces.cli.prices import backfill_prices

    settings = load_settings()
    configure_agent_logging(settings)
    backfill_prices(settings, run, receipt_id, apply=apply)


@catalog_app.command("todo")
def catalog_todo(
    store: StoreOption = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", metavar="N", min=1, help="At most N rows.", show_default=False),
    ] = None,
    as_json: JsonOption = False,
    run: RunOption = None,
) -> None:
    """List the products nobody has confirmed yet (printed or guessed), most bought first."""
    from paper_trail.interfaces.cli.catalog import catalog_todo as todo

    settings = load_settings()
    configure_agent_logging(settings)
    todo(settings, run, store, limit, as_json=as_json)


@catalog_app.command("confirm")
def catalog_confirm(
    product_id: Annotated[
        int, typer.Argument(metavar="PRODUCT_ID", help="The product to confirm.")
    ],
    run: RunOption = None,
) -> None:
    """Accept a product's current name, brand, size, category and tags as a person's."""
    from paper_trail.interfaces.cli.catalog import confirm_product

    settings = load_settings()
    configure_agent_logging(settings)
    confirm_product(settings, run, product_id)


@catalog_app.command("enrich")
def catalog_enrich(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Look up, guess and write (default: dry run)."),
    ] = False,
    run: RunOption = None,
) -> None:
    """Look stored products up in the product databases, else have Claude guess them."""
    from paper_trail.interfaces.cli.catalog import enrich_products

    settings = load_settings()
    configure_agent_logging(settings)
    enrich_products(settings, run, apply=apply)


@catalog_app.command("add")
def catalog_add(  # noqa: PLR0913 - one parameter per command-line option
    store: Annotated[str, typer.Argument(metavar="STORE", help="The store (a name or its key).")],
    code: Annotated[str, typer.Argument(metavar="CODE", help="The code as printed.")],
    name: Annotated[
        str | None,
        typer.Argument(metavar="NAME", help="The product's name.", show_default=False),
    ] = None,
    *,
    product_id: Annotated[
        int | None,
        typer.Option(
            "--product", metavar="ID", help="Make the code name this product.", show_default=False
        ),
    ] = None,
    brand: Annotated[
        str | None, typer.Option("--brand", metavar="B", help="Its brand.", show_default=False)
    ] = None,
    size: Annotated[
        str | None,
        typer.Option(
            "--size", metavar="SIZE", help='Its size, e.g. "1.36 kg".', show_default=False
        ),
    ] = None,
    category: Annotated[
        Category | None,
        typer.Option("--category", metavar="C", help="Its category.", show_default=False),
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="Rename a product a person already named.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Save without asking.")] = False,
    run: RunOption = None,
) -> None:
    """Name the product of a code at a store, or make the code name an existing product."""
    from paper_trail.interfaces.cli.catalog import add_code

    settings = load_settings()
    configure_agent_logging(settings)
    add_code(
        settings,
        run,
        store,
        code,
        name,
        product_id=product_id,
        brand=brand,
        size=size,
        category=category,
        replace=replace,
        yes=yes,
    )


@catalog_app.command("list")
def catalog_list(
    store: StoreOption = None,
    scheme: Annotated[
        CodeScheme | None,
        typer.Option("--scheme", help="Only codes of this scheme.", show_default=False),
    ] = None,
    as_json: JsonOption = False,
    run: RunOption = None,
) -> None:
    """List the stored codes and the products they name."""
    from paper_trail.interfaces.cli.catalog import list_codes

    settings = load_settings()
    configure_agent_logging(settings)
    list_codes(settings, run, store, scheme, as_json=as_json)


@catalog_app.command("remove")
def catalog_remove(
    store: Annotated[
        str, typer.Option("--store", metavar="S", help="The store (a name or its key).")
    ],
    code: Annotated[str, typer.Option("--code", metavar="C", help="The code as printed.")],
    run: RunOption = None,
) -> None:
    """Forget what a code names; the product and its lines are kept."""
    from paper_trail.interfaces.cli.catalog import remove_code

    settings = load_settings()
    configure_agent_logging(settings)
    remove_code(settings, run, store, code)


@catalog_app.command("lookup")
def catalog_lookup(
    raw_text: Annotated[str, typer.Argument(metavar="RAW_TEXT", help="A receipt line as printed.")],
    store: Annotated[
        str, typer.Option("--store", metavar="S", help="The store (a name or its key).")
    ],
    run: RunOption = None,
) -> None:
    """Show how a line's code is read and which source knows it. Writes nothing."""
    from paper_trail.interfaces.cli.catalog import lookup_code

    settings = load_settings()
    configure_agent_logging(settings)
    lookup_code(settings, run, raw_text, store)


@catalog_app.command("plu-import")
def catalog_plu_import(
    file: Annotated[
        Path, typer.Argument(metavar="FILE", help="An IFPS PLU export (.csv).", show_default=False)
    ],
) -> None:
    """Write the PLU table (CATALOG__PLU_PATH) from an IFPS export. Opens no database."""
    from paper_trail.interfaces.cli.catalog import import_plu_file

    settings = load_settings()
    configure_agent_logging(settings)
    import_plu_file(settings, file)


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
    """Print the agents' graph. Needs neither a database nor Claude."""
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
