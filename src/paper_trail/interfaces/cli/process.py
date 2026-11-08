"""``paper-trail process``: receipt photos (or OCR text) through the agents, with a trace per
receipt on the terminal and on the data volume (agent-core plan 4).

``--interrupt-after NODE`` pauses each receipt after a node of the graph, and ``--resume ID``
continues a receipt's thread from its last checkpoint (after a pause, a GPU drop or a crash);
``--reprocess`` runs a stored photo's receipt again from its stored OCR text.

Exit codes: 0 every receipt completed, is parked for review or paused; 1 the main database is
not migrated, or ``--resume`` names an unknown receipt or one with nothing to resume (or that
waits for a review); 2 a usage error; 3 a receipt failed (a file that is not an image
included); 4 the LLM host is unreachable or not configured, which stops the batch (the receipt
in progress keeps its checkpoint; the output says how to resume it).
"""

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
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
from paper_trail.domain.enums import ReceiptStatus
from paper_trail.domain.errors import ConflictError, NotFoundError, UnsupportedMediaError
from paper_trail.interfaces.cli.options import Transport, with_transport
from paper_trail.interfaces.cli.receipts import EXIT_INVALID_INPUT, EXIT_UNAVAILABLE, fail
from paper_trail.interfaces.cli.runs import llm_calls, open_run, write_trace
from paper_trail.interfaces.cli.trace_view import ReceiptHeader, Style, header, rows, styler
from paper_trail.ports.trace import RunSummary

EXIT_RECEIPT_FAILED = 3
EXIT_LLM_UNAVAILABLE = 4
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"})


@dataclass(frozen=True, slots=True)
class ProcessOptions:
    run: str | None = None
    max_text_attempts: int | None = None
    feedback: bool = True
    vision: bool = True
    examples: bool = True
    prompt_version: str | None = None
    transport: Transport | None = None  # None: settings.tools.transport
    interrupt_after: str | None = None  # a node of the graph (NODE_NAMES)
    reprocess: bool = False
    verbose: bool = False
    color: bool = True


@dataclass(frozen=True, slots=True)
class _Input:
    path: Path
    is_text: bool = False


@dataclass
class _Tally:
    receipts: int = 0
    completed: int = 0
    needs_review: int = 0
    failed: int = 0
    skipped: int = 0
    llm_calls: int = 0
    text_attempts: list[int] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)

    def summary(self) -> RunSummary:
        attempts = self.text_attempts
        return RunSummary(
            receipts=self.receipts,
            completed=self.completed,
            needs_review=self.needs_review,
            failed=self.failed,
            skipped=self.skipped,
            llm_calls=self.llm_calls,
            mean_text_attempts=statistics.fmean(attempts) if attempts else 0.0,
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
    if options.max_text_attempts is not None:
        graph["max_text_attempts"] = options.max_text_attempts
    if not options.vision:
        graph["max_vision_attempts"] = 0
    prompts: dict[str, object] = {"examples": settings.prompts.examples and options.examples}
    if options.prompt_version is not None:
        prompts["extraction_version"] = options.prompt_version
    flagged = settings.model_copy(
        update={
            "graph": settings.graph.model_copy(update=graph),
            "prompts": settings.prompts.model_copy(update=prompts),
        }
    )
    return with_transport(flagged, options.transport)


def process_receipts(
    settings: Settings, paths: Sequence[Path], text_file: Path | None, options: ProcessOptions
) -> None:
    """Process receipt images (or one OCR text file) as a batch."""
    if options.interrupt_after is not None and options.interrupt_after not in NODE_NAMES:
        fail(
            EXIT_INVALID_INPUT,
            f"--interrupt-after {options.interrupt_after!r}: choose one of {', '.join(NODE_NAMES)}",
        )
    inputs, skipped = _inputs(paths, text_file, reprocess=options.reprocess)

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
        prompt_tag = (
            PromptLibrary()
            .load(EXTRACTION_PROMPT, prompts.extraction_version, examples=prompts.examples)
            .tag
        )
    except LookupError:
        fail(
            EXIT_INVALID_INPUT,
            f"--prompt-version {prompts.extraction_version!r}: there is no such prompt",
        )
    batch = _Batch(options, prompt_tag, styler(color=options.color), _Tally(skipped=skipped))
    tally = batch.tally
    with open_run(settings, options.run) as agents:
        try:
            work(agents, batch)
        except ProcessingStoppedError as stopped:
            agents.trace_writer.write_summary(tally.summary())
            fail(
                EXIT_LLM_UNAVAILABLE,
                f"{stopped}; stopped. Receipt {stopped.receipt_id} keeps its checkpoint:"
                f" {resume_hint(stopped.receipt_id, options.run)}",
            )
        summary_path = agents.trace_writer.write_summary(tally.summary())
    _echo_summary(tally, summary_path)
    if tally.failed:
        raise typer.Exit(EXIT_RECEIPT_FAILED)


def resume_hint(receipt_id: int, run: str | None) -> str:
    """The command that continues a stopped or paused receipt."""
    return f"paper-trail process --resume {receipt_id}" + ("" if run is None else f" --run {run}")


def _inputs(
    paths: Sequence[Path], text_file: Path | None, *, reprocess: bool
) -> tuple[list[_Input], list[Path]]:
    """What to process, and the files of the given directories that are not receipt images."""
    if text_file is not None:
        if paths:
            fail(EXIT_INVALID_INPUT, "give either PATH... or --text-file, not both")
        if reprocess:
            fail(EXIT_INVALID_INPUT, "--reprocess is for stored photos; a text is never stored")
        if not text_file.is_file():
            fail(EXIT_INVALID_INPUT, f"{text_file}: no such file")
        return [_Input(text_file, is_text=True)], []
    if not paths:
        fail(EXIT_INVALID_INPUT, "nothing to process: give PATH... or --text-file FILE")
    inputs: list[_Input] = []
    skipped: list[Path] = []
    for path in paths:
        if path.is_dir():
            for child in sorted(child for child in path.iterdir() if child.is_file()):
                if child.suffix.lower() in IMAGE_SUFFIXES:
                    inputs.append(_Input(child))
                else:
                    skipped.append(child)
        elif path.is_file():
            inputs.append(_Input(path))
        else:
            fail(EXIT_INVALID_INPUT, f"{path}: no such file or directory")
    return inputs, skipped


def _process_one(agents: AgentContainer, item: _Input, batch: _Batch) -> None:
    source = (
        ReceiptSource(
            name=item.path.name, text=item.path.read_text(encoding="utf-8", errors="replace")
        )
        if item.is_text
        else ReceiptSource(name=item.path.name, image=item.path.read_bytes())
    )
    options = batch.options
    try:
        _run_one(
            agents,
            batch,
            item.path.name,
            lambda: agents.process_receipt(
                source, reprocess=options.reprocess, interrupt_after=options.interrupt_after
            ),
        )
    except UnsupportedMediaError as error:
        batch.tally.receipts += 1
        batch.tally.failed += 1
        typer.echo(batch.style(f"  ✖ FAILED {item.path.name}: {error}", "red"))


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
        typer.echo(style(f"  ⏹ PROCESSING: stopped · {hint} · trace {trace_path}", "yellow"))
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
    tally.text_attempts.append(graph.text_attempts)
    if graph.status is ReceiptStatus.COMPLETED:
        tally.completed += 1
    elif graph.status is ReceiptStatus.NEEDS_REVIEW:
        tally.needs_review += 1


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
        f" · attempts {graph.text_attempts} · trace {trace_path}",
        "green",
    )


def _echo_summary(tally: _Tally, summary_path: str) -> None:
    median = statistics.median(tally.seconds) if tally.seconds else 0.0
    summary = tally.summary()
    typer.echo(
        f"run: {summary.receipts} receipts · {summary.completed} completed"
        f" · {summary.needs_review} review · {summary.failed} failed · {summary.skipped} skipped"
        f" · mean text attempts {summary.mean_text_attempts:.2f} · llm {summary.llm_calls}"
        f" · median {median:.1f}s · {summary_path}"
    )
