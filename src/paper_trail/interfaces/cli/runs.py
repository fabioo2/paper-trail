"""Named runs of the agent CLI: their database, their traces (agent-core plan 4.1, 4.3).

``--run NAME`` isolates an experiment arm: its own database ``<DATA_DIR>/runs/NAME/`` (migrated
automatically) and traces beside it. Without it the main database is used, which must already
be migrated, and the traces go to ``runs/main``. Images are shared by every run.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from paper_trail.bootstrap import (
    RUNS_DIRNAME,
    Container,
    build_container,
    database_is_current,
    dispose,
    upgrade_database,
)
from paper_trail.bootstrap.agents import AgentContainer, agent_container
from paper_trail.config import DATABASE_FILENAME, Settings
from paper_trail.domain.trace import TraceKind
from paper_trail.interfaces.cli.receipts import (
    EXIT_INVALID_INPUT,
    EXIT_UNAVAILABLE,
    NOT_MIGRATED,
    fail,
)
from paper_trail.interfaces.mcp.server import serve_on_loopback
from paper_trail.ports.agents import GraphOutcome
from paper_trail.ports.trace import ReceiptRun

MAIN_RUN = "main"
RUN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def check_run_name(run: str | None) -> None:
    if run is not None and not RUN_NAME.fullmatch(run):
        fail(EXIT_INVALID_INPUT, f"--run {run!r}: use letters, digits, '.', '_' or '-' (max 64)")


@contextmanager
def open_run(settings: Settings, run: str | None) -> Iterator[AgentContainer]:
    """The agents over the run's database. The Claude spend is always kept in the main
    database, so a named run never gets a budget of its own: the main database must be migrated
    whatever the run (exit 1 otherwise; only a named run's own database is migrated here). With
    ``TOOLS__TRANSPORT=mcp`` the run's tools are served over MCP on a loopback port in this
    process and called through it."""
    ledger_path = settings.database_path
    require_main_migrated(settings, run)
    run_settings = _prepared(settings, run)
    with agent_container(
        run_settings, run or MAIN_RUN, tool_server=serve_on_loopback, ledger_path=ledger_path
    ) as agents:
        yield agents


def require_main_migrated(settings: Settings, run: str | None) -> None:
    """For a named run, exit 1 unless the main database is migrated: the Claude spend is kept
    there whatever the run (review C1). Without a run ``_prepared`` checks it anyway."""
    if run is not None:
        _prepared(settings, None)


@contextmanager
def open_run_database(settings: Settings, run: str | None) -> Iterator[Container]:
    """The application over the run's database, for commands that need no agents."""
    container = build_container(_prepared(settings, run))
    try:
        yield container
    finally:
        dispose(container)


def _prepared(settings: Settings, run: str | None) -> Settings:
    """Settings on the run's database, which is migrated (a named run's automatically; exit
    1 when the main one is not)."""
    check_run_name(run)
    if run is not None:
        database = settings.data_dir / RUNS_DIRNAME / run / DATABASE_FILENAME
        settings = settings.model_copy(update={"database_path": database})
    container = build_container(settings)
    try:
        if run is not None:
            upgrade_database(container)
        elif not database_is_current(container):
            fail(EXIT_UNAVAILABLE, NOT_MIGRATED)
    finally:
        dispose(container)
    return settings


def write_trace(  # noqa: PLR0913 - one receipt's run, described for its trace file
    agents: AgentContainer,
    receipt_id: int,
    source_name: str,
    started_at: datetime,
    graph: GraphOutcome | None,
    *,
    resumed_from: str | None = None,
) -> str:
    """Write the receipt's trace (``runs/<run>/traces``) and return where it went."""
    receipt, _ = agents.container.get_receipt(receipt_id)
    return agents.trace_writer.write_receipt(
        ReceiptRun(
            receipt_id=receipt_id,
            source_name=source_name,
            image_sha256=receipt.image_sha256,
            started_at=started_at,
            finished_at=agents.container.clock.now(),
            status=receipt.status if graph is None else graph.status,
            review_id=None if graph is None else graph.review_id,
            review_reason=None if graph is None else graph.review_reason,
            resumed_from=resumed_from,
            attempts=0 if graph is None else graph.attempts,
            events=agents.tracer.events(receipt_id),
        )
    )


def llm_calls(agents: AgentContainer, receipt_id: int) -> int:
    return sum(1 for event in agents.tracer.events(receipt_id) if event.kind is TraceKind.LLM)
