"""``paper-trail items correct``: a person relinks a receipt line, and matching remembers it
(S4-AC-06, CLI form; the HTTP form comes with Stage 6). ``paper-trail items merge``: two
products are the same one.

Exit codes of ``correct``: 0 corrected; 1 an unknown receipt, line or product, or the main
database is not migrated; 2 not exactly one of ``--product-id``/``--new-product`` (an unknown
``--category`` is refused by Typer, also with 2). Of ``merge``: 0 shown (a dry run) or merged
(``--apply``); 1 an unknown product, a receipt being read, or the main database not migrated;
2 a product merged into itself or a missing ``--into``. No model is called.
"""

import typer

from paper_trail.application.products import ProductMerge
from paper_trail.config import Settings
from paper_trail.domain.enums import Category
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
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


def merge_products(
    settings: Settings, run: str | None, product_id: int, into_id: int, *, apply: bool
) -> None:
    with open_run_database(settings, run) as container:
        merge = container.merge_products if apply else container.merge_products.preview
        try:
            merged = merge(product_id, into_id)
        except DomainValidationError as error:
            fail(EXIT_INVALID_INPUT, str(error))
        except (NotFoundError, ConflictError) as error:
            fail(EXIT_UNAVAILABLE, str(error))
    if apply:
        typer.echo(
            f"{_named(merged)} merged into product {merged.kept.id} {merged.kept.canonical_name}:"
            f" {merged.purchases} purchases moved"
        )
        return
    typer.echo(
        f"{_named(merged)} would be merged into product {merged.into.id}"
        f" {merged.into.canonical_name}:"
    )
    typer.echo(f"  kept as: {merged.kept.canonical_name}")
    typer.echo(
        f"  moving: {merged.purchases} purchases, {merged.codes} codes, {merged.aliases} aliases"
    )
    typer.echo("A merge can only be undone by restoring a backup. Run again with --apply.")


def _named(merge: ProductMerge) -> str:
    return f"product {merge.merged.id} {merge.merged.canonical_name}"
