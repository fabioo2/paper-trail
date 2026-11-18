"""``paper-trail process``: receipt photos through the agents, with a trace per receipt on the
terminal and on the data volume (agent-core plan 4).

``--interrupt-after NODE`` pauses each receipt after a node of the graph, and ``--resume ID``
continues a receipt's thread from its last checkpoint (after a pause, a stopped read or a
crash); ``--reprocess`` has Claude read a stored photo again.

Exit codes: 0 every receipt completed, is parked for review or paused; 1 the main database is
not migrated, ``--resume`` names an unknown receipt or one with nothing to resume (or that
waits for a review), or ``--resume`` or ``--reprocess`` meets a receipt that the worker or
another command is processing (or, reprocessed, one without a stored photo): the batch stops
there; 2 a usage error, or ``--resume`` of a run saved by an older version; 3 a receipt failed
(a file that is not an image included); 4 a Claude read stopped the batch (the
API failed or refused, the answer was cut off, there is no key, or the monthly budget is
reached): the receipt in progress is failed and keeps its checkpoint; the output says how to
resume it.
"""

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import typer

from paper_trail.agents.graph import NODE_NAMES
from paper_trail.agents.nodes.extraction import EXTRACTION_PROMPT
from paper_trail.agents.prompts import PromptLibrary
from paper_trail.application.process_receipt import (
    ProcessingStoppedError,
    ProcessOutcome,
    ReceiptSource,
)
from paper_trail.bootstrap.agents import AgentContainer
from paper_trail.config import Settings
from paper_trail.domain.enums import LineKind, ReceiptStatus
from paper_trail.domain.errors import ConflictError, NotFoundError, UnsupportedMediaError
from paper_trail.domain.models import Receipt, ReceiptDraft
from paper_trail.domain.money import sum_money
from paper_trail.domain.rules.arithmetic import check_arithmetic
from paper_trail.domain.rules.budget import call_cost_usd
from paper_trail.domain.trace import TraceKind
from paper_trail.interfaces.cli.options import Transport, with_transport
from paper_trail.interfaces.cli.receipts import EXIT_INVALID_INPUT, EXIT_UNAVAILABLE, fail
from paper_trail.interfaces.cli.runs import llm_calls, open_run, write_trace
from paper_trail.interfaces.cli.trace_view import ReceiptHeader, Style, header, rows, styler
from paper_trail.ports.agents import OutdatedCheckpointError
from paper_trail.ports.trace import RunSummary

EXIT_RECEIPT_FAILED = 3
EXIT_READER_STOPPED = 4
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"})


@dataclass(frozen=True, slots=True)
class ProcessOptions:
    run: str | None = None
    max_attempts: int | None = None
    feedback: bool = True
    catalog: bool = True  # False: --no-catalog (CATALOG__ENABLED=false for this run)
    transport: Transport | None = None  # None: settings.tools.transport
    interrupt_after: str | None = None  # a node of the graph (NODE_NAMES)
    reprocess: bool = False
    verbose: bool = False
    color: bool = True
    table: bool = False  # end with a row per receipt (``make smoke-claude``)


@dataclass(frozen=True, slots=True)
class _Row:
    """One receipt in the ``--table`` summary."""

    name: str
    store: str
    items: int
    lines: str  # the sum of the stored lines
    subtotal: str
    total: str
    adds_up: bool
    models: str
    attempts: int
    cost_usd: Decimal


@dataclass
class _Tally:
    receipts: int = 0
    completed: int = 0
    needs_review: int = 0
    failed: int = 0
    skipped: int = 0
    llm_calls: int = 0
    attempts: list[int] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)
    rows: list[_Row] = field(default_factory=list)

    def summary(self) -> RunSummary:
        attempts = self.attempts
        return RunSummary(
            receipts=self.receipts,
            completed=self.completed,
            needs_review=self.needs_review,
            failed=self.failed,
            skipped=self.skipped,
            llm_calls=self.llm_calls,
            mean_attempts=statistics.fmean(attempts) if attempts else 0.0,
        )


@dataclass(frozen=True, slots=True)
class _Batch:
    """What every receipt of one ``process`` invocation shares."""

    options: ProcessOptions
    prompt_tag: str
    style: Style
    tally: _Tally


def with_flags(settings: Settings, options: ProcessOptions) -> Settings:
    """Settings with the command's flags applied; the graph only ever sees Settings."""
    graph: dict[str, object] = {"feedback": settings.graph.feedback and options.feedback}
    if options.max_attempts is not None:
        graph["max_attempts"] = options.max_attempts
    catalog = {"enabled": settings.catalog.enabled and options.catalog}
    flagged = settings.model_copy(
        update={
            "graph": settings.graph.model_copy(update=graph),
            "catalog": settings.catalog.model_copy(update=catalog),
        }
    )
    return with_transport(flagged, options.transport)


def process_receipts(settings: Settings, paths: Sequence[Path], options: ProcessOptions) -> None:
    """Process receipt images as a batch."""
    if options.interrupt_after is not None and options.interrupt_after not in NODE_NAMES:
        fail(
            EXIT_INVALID_INPUT,
            f"--interrupt-after {options.interrupt_after!r}: choose one of {', '.join(NODE_NAMES)}",
        )
    inputs, skipped = _inputs(paths)

    def work(agents: AgentContainer, batch: _Batch) -> None:
        for path in skipped:
            typer.echo(
                batch.style(f"  - skipped {path.name} (not a receipt image)", "bright_black")
            )
        for item in inputs:
            _process_one(agents, item, batch)

    _run_batch(settings, options, len(skipped), work)


def resume_receipt(settings: Settings, receipt_id: int, options: ProcessOptions) -> None:
    """Continue one receipt's thread from its last checkpoint (``--resume``)."""

    def work(agents: AgentContainer, batch: _Batch) -> None:
        try:
            _run_one(
                agents,
                batch,
                f"--resume {receipt_id}",
                lambda: agents.process_receipt.resume(receipt_id),
            )
        except OutdatedCheckpointError as error:
            fail(EXIT_INVALID_INPUT, str(error))
        except (NotFoundError, ConflictError) as error:
            fail(EXIT_UNAVAILABLE, str(error))

    _run_batch(settings, options, 0, work)


def _run_batch(
    settings: Settings,
    options: ProcessOptions,
    skipped: int,
    work: Callable[[AgentContainer, _Batch], None],
) -> None:
    """Run ``work`` on the run's agents, then write and print the batch summary."""
    settings = with_flags(settings, options)
    prompts = settings.prompts
    try:
        # The tag of the prompt the agents will load: the header shows it, as the LLM events do.
        prompt_tag = PromptLibrary().load(EXTRACTION_PROMPT, prompts.extraction_version).tag
    except LookupError:
        fail(
            EXIT_INVALID_INPUT,
            f"PAPER_TRAIL_PROMPTS__EXTRACTION_VERSION {prompts.extraction_version!r}: there is no"
            " such prompt",
        )
    batch = _Batch(options, prompt_tag, styler(color=options.color), _Tally(skipped=skipped))
    tally = batch.tally
    with open_run(settings, options.run) as agents:
        try:
            work(agents, batch)
        except ProcessingStoppedError as stopped:
            agents.trace_writer.write_summary(tally.summary())
            fail(
                EXIT_READER_STOPPED,
                f"{stopped}; stopped. Receipt {stopped.receipt_id} keeps its checkpoint:"
                f" {resume_hint(stopped.receipt_id, options.run)}",
            )
        summary_path = agents.trace_writer.write_summary(tally.summary())
    if options.table:
        _echo_table(tally.rows)
    _echo_summary(tally, summary_path)
    if tally.failed:
        raise typer.Exit(EXIT_RECEIPT_FAILED)


def resume_hint(receipt_id: int, run: str | None) -> str:
    """The command that continues a stopped or paused receipt."""
    return f"paper-trail process --resume {receipt_id}" + ("" if run is None else f" --run {run}")


def _inputs(paths: Sequence[Path]) -> tuple[list[Path], list[Path]]:
    """What to process, and the files of the given directories that are not receipt images."""
    if not paths:
        fail(EXIT_INVALID_INPUT, "nothing to process: give PATH...")
    inputs: list[Path] = []
    skipped: list[Path] = []
    for path in paths:
        if path.is_dir():
            for child in sorted(child for child in path.iterdir() if child.is_file()):
                if child.suffix.lower() in IMAGE_SUFFIXES:
                    inputs.append(child)
                else:
                    skipped.append(child)
        elif path.is_file():
            inputs.append(path)
        else:
            fail(EXIT_INVALID_INPUT, f"{path}: no such file or directory")
    return inputs, skipped


def _process_one(agents: AgentContainer, path: Path, batch: _Batch) -> None:
    source = ReceiptSource(name=path.name, image=path.read_bytes())
    options = batch.options
    try:
        _run_one(
            agents,
            batch,
            path.name,
            lambda: agents.process_receipt(
                source, reprocess=options.reprocess, interrupt_after=options.interrupt_after
            ),
        )
    except ConflictError as error:
        fail(EXIT_UNAVAILABLE, f"{path.name}: {error}")
    except UnsupportedMediaError as error:
        batch.tally.receipts += 1
        batch.tally.failed += 1
        typer.echo(batch.style(f"  ✖ FAILED {path.name}: {error}", "red"))


def _run_one(
    agents: AgentContainer, batch: _Batch, name: str, run: Callable[[], ProcessOutcome]
) -> None:
    """Run one receipt, print its block and result, and write its trace."""
    style, tally = batch.style, batch.tally
    clock = agents.container.clock
    started = clock.now()
    try:
        outcome = run()
    except ProcessingStoppedError as stopped:
        tally.receipts += 1
        _echo_block(agents, stopped.receipt_id, name, batch)
        trace_path = write_trace(agents, stopped.receipt_id, name, started, None)
        hint = resume_hint(stopped.receipt_id, batch.options.run)
        typer.echo(style(f"  ⏹ FAILED: {stopped} · {hint} · trace {trace_path}", "yellow"))
        raise
    if outcome.duplicate:
        _echo_header(agents, outcome.receipt_id, name, batch)
        typer.echo(
            f"  = duplicate: this image is receipt {outcome.receipt_id}; not processed again"
            " (--reprocess runs it again)"
        )
        return
    tally.receipts += 1
    graph = outcome.graph
    _echo_block(agents, outcome.receipt_id, name, batch)
    trace_path = write_trace(
        agents,
        outcome.receipt_id,
        name,
        started,
        graph,
        resumed_from=None if graph is None else graph.resumed_from,
    )
    seconds = (clock.now() - started).total_seconds()
    _tally(tally, outcome, llm_calls(agents, outcome.receipt_id), seconds)
    if graph is not None and graph.status is ReceiptStatus.COMPLETED:
        typer.echo(_receipt_line(agents, outcome.receipt_id))
    if batch.options.table:
        tally.rows.append(_row(agents, outcome, name))
    typer.echo(_result_line(outcome, seconds, trace_path, batch))


def _echo_header(agents: AgentContainer, receipt_id: int, source_name: str, batch: _Batch) -> None:
    info = ReceiptHeader(
        receipt_id=receipt_id,
        source_name=source_name,
        run=agents.run,
        prompt_tag=batch.prompt_tag,
        transport=agents.run_config.transport,
    )
    typer.echo(batch.style(header(info), bold=True))


def _echo_block(agents: AgentContainer, receipt_id: int, source_name: str, batch: _Batch) -> None:
    _echo_header(agents, receipt_id, source_name, batch)
    for line in rows(agents.tracer.events(receipt_id), batch.style, verbose=batch.options.verbose):
        typer.echo(line)


def _tally(tally: _Tally, outcome: ProcessOutcome, calls: int, seconds: float) -> None:
    tally.llm_calls += calls
    tally.seconds.append(seconds)
    graph = outcome.graph
    if graph is None:
        tally.failed += 1
        return
    tally.attempts.append(graph.attempts)
    if graph.status is ReceiptStatus.COMPLETED:
        tally.completed += 1
    elif graph.status is ReceiptStatus.NEEDS_REVIEW:
        tally.needs_review += 1


def _receipt_line(agents: AgentContainer, receipt_id: int) -> str:
    """What was saved, in one line: store, date, items, the discounts' sum and the total."""
    receipt, _ = agents.container.get_receipt(receipt_id)
    items = sum(1 for line in receipt.items if line.kind is LineKind.ITEM)
    discounts = sum_money(
        (line.line_total for line in receipt.items if line.kind is LineKind.DISCOUNT),
        receipt.currency,
    )
    total = "-" if receipt.total is None else receipt.total.amount_text
    return (
        f"  {receipt.store_name} · {receipt.purchased_on or 'no date'} · {items} items"
        f" · discounts {discounts.amount_text} · total {total}"
    )


def _row(agents: AgentContainer, outcome: ProcessOutcome, name: str) -> _Row:
    """The ``--table`` row of a processed receipt: what was saved (for one parked for review,
    the extraction the review holds), and what its reads cost."""
    saved, report = agents.container.get_receipt(outcome.receipt_id)
    pending = (
        agents.runner.pending_draft(outcome.receipt_id)
        if saved.status is ReceiptStatus.NEEDS_REVIEW and not saved.items
        else None
    )
    receipt: Receipt | ReceiptDraft = saved if pending is None else pending
    adds_up = report.ok if pending is None else check_arithmetic(pending).ok
    reads = [
        event.data
        for event in agents.tracer.events(outcome.receipt_id)
        if event.kind is TraceKind.LLM
    ]
    models = [str(data.get("model")) for data in reads]
    cost = sum(
        (
            call_cost_usd(model, tokens_in, tokens_out)
            for model, data in zip(models, reads, strict=True)
            if isinstance(tokens_in := data.get("input_tokens"), int)
            and isinstance(tokens_out := data.get("output_tokens"), int)
        ),
        Decimal(0),
    )
    lines = sum_money((line.line_total for line in receipt.items), receipt.currency)
    return _Row(
        name=name,
        store=receipt.store_name or "-",
        items=sum(1 for line in receipt.items if line.kind is LineKind.ITEM),
        lines=lines.amount_text,
        subtotal="-" if receipt.subtotal is None else receipt.subtotal.amount_text,
        total="-" if receipt.total is None else receipt.total.amount_text,
        adds_up=adds_up,
        models=" > ".join(models) or "-",
        attempts=0 if outcome.graph is None else outcome.graph.attempts,
        cost_usd=cost,
    )


def _echo_table(rows: Sequence[_Row]) -> None:
    typer.echo(
        f"{'receipt':<24} {'store':<20} {'items':>5} {'lines':>9} {'subtotal':>9}"
        f" {'total':>9} {'adds up':<7} {'attempts':>8} {'cost':>9}  models"
    )
    for row in rows:
        typer.echo(
            f"{row.name[:24]:<24} {row.store[:20]:<20} {row.items:>5} {row.lines:>9}"
            f" {row.subtotal:>9} {row.total:>9} {'yes' if row.adds_up else 'NO':<7}"
            f" {row.attempts:>8} {row.cost_usd:>9.4f}  {row.models}"
        )
    total = sum((row.cost_usd for row in rows), Decimal(0))
    typer.echo(f"{len(rows)} receipts · cost {total:.4f} USD")


def _result_line(outcome: ProcessOutcome, seconds: float, trace_path: str, batch: _Batch) -> str:
    graph, style = outcome.graph, batch.style
    if graph is None:
        return style(f"  ✖ FAILED in {seconds:.1f}s: {outcome.failure} · trace {trace_path}", "red")
    if graph.paused_at is not None:
        hint = resume_hint(outcome.receipt_id, batch.options.run)
        return style(f"  ⏸ paused after {graph.paused_at} · {hint} · trace {trace_path}", "yellow")
    if graph.status is ReceiptStatus.NEEDS_REVIEW and graph.review_id is not None:
        review = graph.review_id
        return style(
            f"  ⏸ NEEDS_REVIEW #{review} {graph.review_reason} · resolve: paper-trail review"
            f" resolve {review} (--accept | --file F) · trace {trace_path}",
            "yellow",
        )
    return style(
        f"  ✔ {graph.status.value.upper()} in {seconds:.1f}s"
        f" · attempts {graph.attempts} · trace {trace_path}",
        "green",
    )


def _echo_summary(tally: _Tally, summary_path: str) -> None:
    median = statistics.median(tally.seconds) if tally.seconds else 0.0
    summary = tally.summary()
    typer.echo(
        f"run: {summary.receipts} receipts · {summary.completed} completed"
        f" · {summary.needs_review} review · {summary.failed} failed · {summary.skipped} skipped"
        f" · mean attempts {summary.mean_attempts:.2f} · llm {summary.llm_calls}"
        f" · median {median:.1f}s · {summary_path}"
    )
