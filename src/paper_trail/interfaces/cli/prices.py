"""``paper-trail prices backfill``: apply the discount rule to receipts stored before it
(discounts-and-paid-prices plan, section 9).

A dry run by default: one row per discount line (linked to its item with the regular and paid
price, or kept at receipt level with the reason) and a totals line. ``--apply`` writes the new
links and the observations' discounts. Exit codes: 0 done; 1 the main database is not migrated;
2 an unknown or not completed ``--receipt`` (or a bad ``--run``). No model is called.
"""

import typer

from paper_trail.application.prices import BackfilledDiscount, PriceBackfill
from paper_trail.config import Settings
from paper_trail.domain.errors import DomainValidationError, NotFoundError
from paper_trail.interfaces.cli.receipts import EXIT_INVALID_INPUT, discount_row, fail
from paper_trail.interfaces.cli.runs import open_run_database


def backfill_prices(
    settings: Settings, run: str | None, receipt_id: int | None, *, apply: bool
) -> None:
    with open_run_database(settings, run) as container:
        try:
            backfill = container.backfill_prices(receipt_id=receipt_id, apply=apply)
        except (NotFoundError, DomainValidationError) as error:
            fail(EXIT_INVALID_INPUT, str(error))
    echo_backfill(backfill)


def echo_backfill(backfill: PriceBackfill) -> None:
    """One row per discount, then the totals line."""
    for entry in backfill.discounts:
        row = discount_row(entry.discount, entry.item, entry.reason)
        if entry.item is not None:
            link = "new link" if entry.new_link else "linked before"
            row = f"{row} ({link}, {_price(entry)})"
        typer.echo(f"receipt {entry.receipt_id} {row}")
    linked = sum(entry.item is not None for entry in backfill.discounts)
    receipt_level = len(backfill.discounts) - linked
    verb = "set" if backfill.applied else "to set"
    typer.echo(
        f"{'applied' if backfill.applied else 'dry run (nothing written)'}: {linked} linked"
        f" · {receipt_level} receipt-level · {backfill.links_set} links {verb}"
        f" · {backfill.prices_updated} prices {'updated' if backfill.applied else 'to update'}"
        f" · {backfill.changes} changes"
    )


def _price(entry: BackfilledDiscount) -> str:
    if entry.price_updated:
        return "price updated"
    return "link only" if entry.new_link else "price unchanged"
