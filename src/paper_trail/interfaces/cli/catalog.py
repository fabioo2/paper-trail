"""``paper-trail catalog``: name the products receipts print codes for, and fix stored codes
(barcode-lookup plan section 5; claude-extraction plan D3, D4).

``todo`` lists the products nobody has confirmed (printed or guessed names, coded or not), with
the store's search link to open by hand (never requested here); ``confirm`` makes a product's
values a person's; ``add`` names a code's product; ``list``, ``remove`` and ``lookup`` show and
fix ``product_codes``; ``enrich`` looks stored products up and guesses them (a dry run unless
``--apply``); ``plu-import`` writes ``plu.csv`` from an IFPS export (no database). Every command
but ``plu-import`` takes ``--run NAME``.

Exit codes: 0 done; 1 the main database is not migrated, an unknown product, code or file, a
declined prompt, or a product ``enrich --apply`` failed on; 2 invalid input (an unparsable code
or size, conflicting options, a person's name without ``--replace``, a missing column). Only
``enrich --apply`` calls Claude or the product databases.
"""

import csv
import json
from pathlib import Path

import typer

from paper_trail.application.catalog import (
    CodeNamed,
    CodeNaming,
    ConfirmProduct,
    TodoRow,
    import_plu,
    parse_size,
)
from paper_trail.application.enrichment import EnrichPlan
from paper_trail.bootstrap.agents import catalog_enrichment
from paper_trail.config import Settings
from paper_trail.domain.enums import Category, CodeScheme, FieldSource
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import Product
from paper_trail.interfaces.cli.receipts import EXIT_INVALID_INPUT, EXIT_UNAVAILABLE, fail
from paper_trail.interfaces.cli.runs import open_run_database, require_main_migrated
from paper_trail.ports.enrichment import EnrichOutcome, ProductEnriched

NOTHING_TO_CONFIRM = "nothing to confirm: every product bought has a confirmed name"
NOTHING_TO_ENRICH = "nothing to enrich"
DRY_RUN = "nothing written; rerun with --apply"
SUGGESTED = "suggested"


def catalog_todo(
    settings: Settings, run: str | None, store: str | None, limit: int | None, *, as_json: bool
) -> None:
    """The table (product | name | name source | code | times bought | last paid | last date |
    lookup link; a guessed name's source shows as ``suggested``, no code as ``-``), or
    ``--json``: a list of objects with keys ``product_id``, ``name``, ``name_source``,
    ``code``, ``scheme``, ``store_key``, ``times_bought``, ``last_paid_cents``, ``last_date``,
    ``lookup_url``. An empty table prints one line saying there is nothing to confirm."""
    with open_run_database(settings, run) as container:
        rows = container.catalog_todo(store=store, limit=limit)
    echo_todo(rows, as_json=as_json)


def confirm_product(settings: Settings, run: str | None, product_id: int) -> None:
    """Stamp the product's filled fields ``person`` and print it with its sources; exit 1 for
    an unknown product."""
    with open_run_database(settings, run) as container:
        try:
            product = ConfirmProduct(container.uow_factory)(product_id)
        except NotFoundError as error:
            fail(EXIT_UNAVAILABLE, str(error))
    sources = ", ".join(f"{key} {source}" for key, source in product.field_sources.items())
    typer.echo(f"product {product.id} {_described(product)}")
    typer.echo(f"confirmed: {sources}")


def enrich_products(settings: Settings, run: str | None, *, apply: bool) -> None:
    """Without ``apply``: each product with something to fill, its open fields and the steps
    that may fill them, then ``nothing written; rerun with --apply``. With it: one line per
    product changed or failed, the counts, and how many were skipped for the budget; exit 1
    when a product failed (the others are written). With ``--run`` the main database must be
    migrated too: Claude's spend is kept there."""
    require_main_migrated(settings, run)
    with (
        open_run_database(settings, run) as container,
        catalog_enrichment(container, settings.database_path) as enrich,
    ):
        if not apply:
            echo_plans(enrich.preview())
            return
        done = enrich()
    failed = echo_enriched(done)
    if failed:
        fail(EXIT_UNAVAILABLE, f"enrichment failed for product {', '.join(failed)}")


def add_code(  # noqa: PLR0913 - the command's arguments and options
    settings: Settings,
    run: str | None,
    store: str,
    code: str,
    name: str | None,
    *,
    product_id: int | None,
    brand: str | None,
    size: str | None,
    category: Category | None,
    replace: bool,
    yes: bool,
) -> None:
    """Check the options (exit 2), preview, show the product before and after and ask y/N
    unless ``yes`` (no or end of input: exit 1, nothing written), then name the code."""
    try:
        size_value, size_unit = (None, None) if size is None else parse_size(size)
    except DomainValidationError as error:
        fail(EXIT_INVALID_INPUT, str(error))
    naming = CodeNaming(
        store=store,
        code=code,
        name=name,
        product_id=product_id,
        brand=brand,
        size_value=size_value,
        size_unit=size_unit,
        category=category,
        replace=replace,
    )
    with open_run_database(settings, run) as container:
        try:
            if not yes:
                preview = container.add_catalog_code.preview(naming)
                echo_named(preview)
                if not preview.changed:
                    typer.echo("nothing to change")
                    return
                try:
                    confirmed = typer.confirm("Save?", default=False)
                except typer.Abort:
                    confirmed = False
                if not confirmed:
                    fail(EXIT_UNAVAILABLE, "nothing saved")
            named = container.add_catalog_code(naming)
        except DomainValidationError as error:
            fail(EXIT_INVALID_INPUT, str(error))
        except ConflictError as error:
            fail(EXIT_INVALID_INPUT, f"{error}; give --replace to rename it")
        except NotFoundError as error:
            fail(EXIT_UNAVAILABLE, str(error))
    if yes:
        echo_named(named)
    typer.echo("saved" if named.changed else "nothing to change")


def list_codes(
    settings: Settings,
    run: str | None,
    store: str | None,
    scheme: CodeScheme | None,
    *,
    as_json: bool,
) -> None:
    """One row per code: scheme, store, code, product id, product name, source; ``--json``:
    objects with keys ``scheme``, ``store_key``, ``code``, ``product_id``, ``product_name``,
    ``source``."""
    with open_run_database(settings, run) as container:
        rows = container.list_catalog_codes(store=store, scheme=scheme)
    if as_json:
        typer.echo(
            json.dumps(
                [
                    {
                        "scheme": row.code.scheme.value,
                        "store_key": row.code.store_key,
                        "code": row.code.code,
                        "product_id": row.code.product_id,
                        "product_name": row.product_name,
                        "source": row.code.source.value,
                    }
                    for row in rows
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    for row in rows:
        typer.echo(
            f"{row.code.scheme} | {row.code.store_key or '-'} | {row.code.code}"
            f" | product {row.code.product_id} | {row.product_name} | {row.code.source}"
        )
    typer.echo(f"{len(rows)} code{'' if len(rows) == 1 else 's'}")


def remove_code(settings: Settings, run: str | None, store: str, code: str) -> None:
    with open_run_database(settings, run) as container:
        try:
            removed = container.remove_catalog_code(store, code)
        except DomainValidationError as error:
            fail(EXIT_INVALID_INPUT, str(error))
        except NotFoundError as error:
            fail(EXIT_UNAVAILABLE, str(error))
    typer.echo(f"removed {removed.identity}; product {removed.product_id} is kept")


def lookup_code(settings: Settings, run: str | None, raw_text: str, store: str) -> None:
    """The code's kind, identity, lookup keys, and which source hits (``store catalog`` with
    the product id and name, ``plu`` with its name, ``no source``), or ``no code``."""
    with open_run_database(settings, run) as container:
        found = container.lookup_catalog_code(raw_text, store)
    if found.code is None:
        typer.echo("no code")
        return
    typer.echo(f"kind: {found.code.kind}")
    identity = "no identity" if found.identity is None else found.identity
    typer.echo(f"identity: {identity}")
    typer.echo(f"lookup keys: {', '.join(found.code.keys) or '-'}")
    entry = found.entry
    if entry is None:
        typer.echo("source: no source")
    elif entry.product_id is not None:
        typer.echo(f"source: {entry.source} · product {entry.product_id} {entry.name}")
    else:
        typer.echo(f"source: {entry.source} · {entry.name}")


def import_plu_file(settings: Settings, file: Path) -> None:
    """Write ``settings.catalog.plu_path`` from ``file``; prints e.g. ``1435 imported, 113
    skipped (111 retailer assigned, 2 duplicate)``."""
    target = settings.catalog.plu_path
    if target is None:
        msg = "Settings fills in CATALOG__PLU_PATH"
        raise ValueError(msg)
    try:
        imported = import_plu(file, target)
    except DomainValidationError as error:
        fail(EXIT_INVALID_INPUT, str(error))
    except NotFoundError as error:
        fail(EXIT_UNAVAILABLE, str(error))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        fail(EXIT_UNAVAILABLE, f"{file} could not be read or {target} written: {error}")
    skipped = sum(imported.skipped.values())
    reasons = ", ".join(f"{count} {reason}" for reason, count in imported.skipped.items())
    typer.echo(f"wrote {imported.path}")
    typer.echo(
        f"{imported.imported} imported, {skipped} skipped" + (f" ({reasons})" if reasons else "")
    )


def echo_plans(plans: list[EnrichPlan]) -> None:
    if not plans:
        typer.echo(NOTHING_TO_ENRICH)
    for plan in plans:
        typer.echo(
            f"product {plan.product_id} {plan.name} | open: {', '.join(plan.open_fields)}"
            f" | {' then '.join(plan.steps)}"
        )
    typer.echo(DRY_RUN)


def echo_enriched(done: list[ProductEnriched]) -> list[str]:
    """Print each changed or failed product and the counts; return the failed products' ids."""
    counts = dict.fromkeys(EnrichOutcome, 0)
    failed: list[str] = []
    for result in done:
        counts[result.outcome] += 1
        if result.outcome is EnrichOutcome.FAILED:
            failed.append(str(result.product_id))
            typer.echo(f"product {result.product_id}: failed: {result.error}")
        elif result.written:
            fields = ", ".join(f"{key} {source}" for key, source in result.written.items())
            typer.echo(f"product {result.product_id}: {result.outcome}: {fields}")
    typer.echo(
        f"looked up {counts[EnrichOutcome.LOOKED_UP]}, guessed {counts[EnrichOutcome.GUESSED]},"
        f" unchanged {counts[EnrichOutcome.UNCHANGED]}, failed {counts[EnrichOutcome.FAILED]}"
    )
    skipped = counts[EnrichOutcome.SKIPPED]
    if skipped:
        typer.echo(f"{skipped} products skipped: monthly Claude budget reached")
    return failed


def _name_source(source: FieldSource | None) -> str:
    if source is None:
        return "-"
    return SUGGESTED if source is FieldSource.LLM else source.value


def echo_todo(rows: tuple[TodoRow, ...], *, as_json: bool) -> None:
    if as_json:
        typer.echo(
            json.dumps(
                [
                    {
                        "product_id": row.product_id,
                        "name": row.name,
                        "name_source": None if row.name_source is None else row.name_source.value,
                        "code": None if row.code is None else row.code.code,
                        "scheme": None if row.code is None else row.code.scheme.value,
                        "store_key": None if row.code is None else row.code.store_key,
                        "times_bought": row.times_bought,
                        "last_paid_cents": None
                        if row.last_paid is None
                        else row.last_paid.amount_cents,
                        "last_date": None if row.last_date is None else row.last_date.isoformat(),
                        "lookup_url": row.lookup_url,
                    }
                    for row in rows
                ],
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    if not rows:
        typer.echo(NOTHING_TO_CONFIRM)
        return
    typer.echo(
        "product | name | name source | code | times bought | last paid | last date | lookup link"
    )
    for row in rows:
        paid = "-" if row.last_paid is None else row.last_paid.amount_text
        bought = "-" if row.last_date is None else row.last_date.isoformat()
        code = "-" if row.code is None else row.code.code
        typer.echo(
            f"{row.product_id} | {row.name} | {_name_source(row.name_source)} | {code}"
            f" | {row.times_bought} | {paid} | {bought} | {row.lookup_url or '-'}"
        )


def echo_named(named: CodeNamed) -> None:
    """The code, and the product before -> after (or the product left as it is)."""
    typer.echo(f"code: {named.code} ({named.source})")
    if named.before is None:
        typer.echo("before: the code names no product")
    else:
        typer.echo(f"before: product {named.before.id} {_described(named.before)}")
    after = "new product" if named.after.id is None else f"product {named.after.id}"
    typer.echo(f"after: {after} {_described(named.after)}")
    if named.left is not None:
        typer.echo(
            f"left as it is: product {named.left.id} {named.left.canonical_name}"
            f" with its {named.left_lines} lines (not moved)"
        )


def _size(product: Product) -> str | None:
    if product.size_value is None:
        return None
    return f"{product.size_value} {product.size_unit or ''}".rstrip()


def _described(product: Product) -> str:
    size = "" if product.size_value is None else f" · {_size(product)}"
    brand = "" if product.brand is None else f" · {product.brand}"
    category = "" if product.category is None else f" · {product.category}"
    return f'"{product.canonical_name}"{brand}{size}{category}'
