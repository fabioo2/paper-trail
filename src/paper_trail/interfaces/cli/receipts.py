"""``paper-trail receipts``: import a receipt from JSON, show one as JSON (PLAN 3.8).

The commands are registered by ``main``, which owns settings and logging; these functions do
the work on a built container and end with ``typer.Exit`` codes: 0 done (a receipt that needs
review is still recorded), 1 the database is not usable or the receipt is unknown, 2 invalid
input.
"""

from pathlib import Path
from typing import NoReturn

import typer
from pydantic import ValidationError

from paper_trail.bootstrap import Container, database_is_current
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import ReceiptDraft
from paper_trail.domain.rules.arithmetic import ArithmeticReport
from paper_trail.interfaces.cli.schemas import ReceiptImport
from paper_trail.interfaces.web.schemas.receipts import ReceiptDetailOut

EXIT_UNAVAILABLE = 1
EXIT_INVALID_INPUT = 2

NOT_MIGRATED = "the database is not migrated; run `paper-trail db upgrade` first"


def import_receipt(container: Container, file: Path, *, dry_run: bool) -> None:
    """Record the receipt in ``file`` and print its id, status and arithmetic issues."""
    draft = read_draft(file)
    if dry_run:
        assessment = container.record_receipt.assess(draft)
        typer.echo(f"dry run: {assessment.status}")
        echo_issues(assessment.arithmetic)
        return
    _require_current_database(container)
    result = container.record_receipt(draft)
    typer.echo(f"receipt {result.receipt.id}: {result.receipt.status}")
    echo_issues(result.arithmetic)


def show_receipt(container: Container, receipt_id: int) -> None:
    """Print the receipt as the JSON document ``GET /api/receipts/{id}`` returns."""
    _require_current_database(container)
    try:
        receipt, report = container.get_receipt(receipt_id)
    except NotFoundError as error:
        fail(EXIT_UNAVAILABLE, str(error))
    typer.echo(ReceiptDetailOut.from_receipt(receipt, report).model_dump_json(indent=2))


def read_draft(file: Path) -> ReceiptDraft:
    """The draft in ``file`` (the ``ReceiptImport`` format); exit 2 naming each bad field."""
    try:
        return ReceiptImport.model_validate_json(file.read_bytes()).to_draft()
    except ValidationError as error:
        problems = "\n".join(
            f"  {'.'.join(str(part) for part in detail['loc']) or '(file)'}: {detail['msg']}"
            for detail in error.errors()
        )
        fail(EXIT_INVALID_INPUT, f"{file} is not a valid receipt import:\n{problems}")


def _require_current_database(container: Container) -> None:
    if not database_is_current(container):
        fail(EXIT_UNAVAILABLE, NOT_MIGRATED)


def echo_issues(report: ArithmeticReport) -> None:
    for issue in report.issues:
        typer.echo(f"  {issue.message}")


def fail(code: int, message: str) -> NoReturn:
    """Print ``message`` on stderr and exit with ``code``."""
    typer.echo(f"paper-trail: {message}", err=True)
    raise typer.Exit(code)
