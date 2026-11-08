"""``paper-trail items correct``: a person relinks a receipt line, and matching remembers it
(S4-AC-06, CLI form; the HTTP form comes with Stage 6).

Exit codes: 0 corrected; 1 an unknown receipt, line or product, or the main database is not
migrated; 2 not exactly one of ``--product-id``/``--new-product`` (an unknown ``--category`` is
refused by Typer, also with 2). No model is called.
"""

import typer

from paper_trail.config import Settings
from paper_trail.domain.enums import Category
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import require_id
from paper_trail.interfaces.cli.receipts import EXIT_INVALID_INPUT, EXIT_UNAVAILABLE, fail
from paper_trail.interfaces.cli.runs import open_run_database


def correct_item(  # noqa: PLR0913 - the command's arguments and options
    settings: Settings,
    run: str | None,
    receipt_id: int,
    position: int,
    *,
    product_id: int | None,
    new_product: str | None,
    category: Category | None,
) -> None:
    if (product_id is None) == (new_product is None):
        fail(EXIT_INVALID_INPUT, "give exactly one of --product-id N or --new-product NAME")
    with open_run_database(settings, run) as container:
        try:
            line = container.correct_line_item(
                receipt_id,
                position,
                product_id=product_id,
                new_product_name=new_product,
                category=category,
            )
            product = container.get_product(require_id(line.product_id))
        except NotFoundError as error:
            fail(EXIT_UNAVAILABLE, str(error))
    typer.echo(
        f"receipt {receipt_id} line {position}: product {product.id} {product.canonical_name}"
    )
