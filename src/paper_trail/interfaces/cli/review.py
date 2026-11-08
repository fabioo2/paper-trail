"""``paper-trail review list | show | resolve``: the human in the loop (S3-AC-07, AC-A-08).

Resolving resumes the receipt's parked graph thread, in this process, from its checkpoint.
Exit codes: 0 done; 1 an unknown review or one that is no longer open; 2 invalid input (an
inconsistent draft without ``--force``, ``--force`` without ``--reason``, not exactly one of
``--file``/``--accept``, an invalid file); 4 the LLM host is unreachable.
"""

from pathlib import Path

import typer

from paper_trail.application.receipts import InconsistentDraftError
from paper_trail.config import Settings
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import JsonValue, ReviewItem, require_id
from paper_trail.interfaces.cli.process import EXIT_LLM_UNAVAILABLE
from paper_trail.interfaces.cli.receipts import (
    EXIT_INVALID_INPUT,
    EXIT_UNAVAILABLE,
    echo_issues,
    fail,
    read_draft,
)
from paper_trail.interfaces.cli.runs import open_run, write_trace
from paper_trail.ports.llm import LlmUnavailableError


def list_reviews(settings: Settings, run: str | None) -> None:
    with open_run(settings, run) as agents:
        reviews = agents.list_open_reviews()
    if not reviews:
        typer.echo("no open reviews")
    for review in reviews:
        typer.echo(
            f"#{review.id}  receipt {review.receipt_id}  {review.reason.value}"
            f"  {_issue_count(review)} issues  {review.created_at:%Y-%m-%d %H:%M}"
        )


def show_review(settings: Settings, run: str | None, review_id: int) -> None:
    with open_run(settings, run) as agents:
        try:
            review = agents.get_review(review_id)
        except NotFoundError as error:
            fail(EXIT_UNAVAILABLE, str(error))
        pending = agents.runner.pending_draft(review.receipt_id)
    typer.echo(f"review #{review.id} · receipt {review.receipt_id} · {review.status.value}")
    typer.echo(f"reason: {review.reason.value}")
    for message in _issue_messages(review):
        typer.echo(f"  {message}")
    if pending is None:
        typer.echo("pending extraction: none (resolve with --file)")
    else:
        typer.echo(
            f"pending extraction: {pending.store_name} · {len(pending.items)} lines"
            f" · total {pending.total.amount_text} {pending.total.currency}"
        )


def resolve_review(  # noqa: PLR0913 - the command's options
    settings: Settings,
    run: str | None,
    review_id: int,
    file: Path | None,
    *,
    accept: bool,
    force: bool,
    reason: str | None,
) -> None:
    if (file is None) == (not accept):
        fail(EXIT_INVALID_INPUT, "give exactly one of --file F or --accept")
    if force and not (reason and reason.strip()):
        fail(EXIT_INVALID_INPUT, "--force needs --reason TEXT: why the receipt does not add up")
    draft = None if file is None else read_draft(file)
    with open_run(settings, run) as agents:
        started = agents.container.clock.now()
        try:
            receipt = agents.resolve_review(review_id, draft, force=force, reason=reason)
        except InconsistentDraftError as error:
            typer.echo("the receipt does not add up (use --force --reason TEXT to keep it):")
            echo_issues(error.report)
            raise typer.Exit(EXIT_INVALID_INPUT) from None
        except DomainValidationError as error:
            fail(EXIT_INVALID_INPUT, str(error))
        except (NotFoundError, ConflictError) as error:
            fail(EXIT_UNAVAILABLE, str(error))
        except LlmUnavailableError as error:
            fail(EXIT_LLM_UNAVAILABLE, str(error))
        receipt_id = require_id(receipt.id)
        write_trace(
            agents, receipt_id, f"review #{review_id}", started, None, resumed_from="review"
        )
    typer.echo(f"receipt {receipt_id}: {receipt.status.value}")


def _issues(review: ReviewItem) -> list[JsonValue]:
    issues = review.details.get("issues")
    return issues if isinstance(issues, list) else []


def _issue_count(review: ReviewItem) -> int:
    return len(_issues(review))


def _issue_messages(review: ReviewItem) -> list[str]:
    return [str(issue.get("message")) for issue in _issues(review) if isinstance(issue, dict)]
