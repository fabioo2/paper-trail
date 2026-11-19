"""Merging a product into the same one (``MergeProducts``): the kept product's fields, the
refusals, and enrichment that read the merged product, on a scratch database.

Words: S is the product merged away, K the product kept.

Pinned: per field (the size's value and unit together) K's value stays unless S's ranks higher
(a person over a product database or the PLU table over printed, guessed or unsourced, anything
over empty), and the winner brings its source; ties keep K's. S's losing name is written
nowhere, and a winning name is copied as stored. A product merged into itself, an unknown
product, a job waiting or running write nothing. Enrichment queued for S writes nothing once S
is merged away.
"""

import re
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from paper_trail.application.enrichment import ProductEnricher, Throttle
from paper_trail.application.products import (
    MergeProducts,
    busy_message,
    merged_fields,
    new_product,
)
from paper_trail.domain.enums import Category, FieldSource, JobKind, Unit
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import Product
from paper_trail.ports.enrichment import EnrichOutcome, EnrichTarget
from paper_trail.ports.lookups import LookupHit
from paper_trail.ports.unit_of_work import UnitOfWork
from tests.factories import MASKED_TEST_PAN, TEST_PAN, product, receipt
from tests.fakes.catalog import FakeCatalog
from tests.fakes.clock import FakeClock
from tests.fakes.lookups import FakeLookup
from tests.storage import ScratchDatabase, in_unit, scratch_database
from tests.support import require_id

TEXT, LLM, PERSON, OFF, PLU, UPC = (
    FieldSource.RECEIPT_TEXT,
    FieldSource.LLM,
    FieldSource.PERSON,
    FieldSource.OFF,
    FieldSource.PLU,
    FieldSource.UPC,
)
EMPTY = product(
    canonical_name="Name",
    brand=None,
    size_value=None,
    size_unit=None,
    category=None,
    tags=(),
    field_sources={},
)
GTIN = "4006381333931"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def database(clock: FakeClock) -> ScratchDatabase:
    return scratch_database(clock)


def _stored(database: ScratchDatabase, item: Product) -> int:
    return require_id(in_unit(database.uow_factory, lambda uow: uow.products.add(item)).id)


def _brand(value: str | None, source: FieldSource | None) -> Product:
    return replace(EMPTY, brand=value, field_sources={} if source is None else {"brand": source})


def _size(value: int, unit: Unit, source: FieldSource) -> Product:
    return replace(EMPTY, size_value=Decimal(value), size_unit=unit, field_sources={"size": source})


def _name(value: str, source: FieldSource) -> Product:
    return replace(EMPTY, canonical_name=value, field_sources={"canonical_name": source})


@pytest.mark.parametrize(
    ("kept", "merged", "expected"),
    [
        (_brand(None, None), _brand("S", TEXT), _brand("S", TEXT)),
        (_brand(None, None), _brand("S", None), _brand("S", None)),
        (
            replace(_brand(None, None), field_sources={"brand": LLM}),
            _brand("S", None),
            _brand("S", None),
        ),
        (_brand("K", LLM), _brand("S", PERSON), _brand("S", PERSON)),
        (_brand("K", TEXT), _brand("S", OFF), _brand("S", OFF)),
        (_brand("K", PERSON), _brand("S", OFF), _brand("K", PERSON)),
        (_brand("K", OFF), _brand("S", PERSON), _brand("S", PERSON)),
        (_brand("K", LLM), _brand("S", LLM), _brand("K", LLM)),
        (_brand("K", PERSON), _brand("S", PERSON), _brand("K", PERSON)),
        (_brand("K", PLU), _brand("S", UPC), _brand("K", PLU)),
        (_brand("K", None), _brand("S", LLM), _brand("K", None)),
        (_brand(None, None), _brand(None, None), _brand(None, None)),
        (_size(500, Unit.G, LLM), _size(1, Unit.KG, OFF), _size(1, Unit.KG, OFF)),
        (_size(500, Unit.G, PERSON), _size(1, Unit.KG, OFF), _size(500, Unit.G, PERSON)),
        (_name("Kept", TEXT), _name("Merged", PERSON), _name("Merged", PERSON)),
        (_name("Kept", PERSON), _name("Merged", TEXT), _name("Kept", PERSON)),
        (
            replace(EMPTY, category=Category.PANTRY, field_sources={"category": TEXT}),
            replace(EMPTY, category=Category.PRODUCE, field_sources={"category": PLU}),
            replace(EMPTY, category=Category.PRODUCE, field_sources={"category": PLU}),
        ),
        (
            replace(EMPTY, tags=("kept",), field_sources={"tags": PERSON}),
            replace(EMPTY, tags=("merged", "red"), field_sources={"tags": OFF}),
            replace(EMPTY, tags=("kept",), field_sources={"tags": PERSON}),
        ),
        (
            EMPTY,
            replace(EMPTY, tags=("merged",), field_sources={"tags": LLM}),
            replace(EMPTY, tags=("merged",), field_sources={"tags": LLM}),
        ),
    ],
)
def test_each_field_keeps_the_better_sourced_value_with_its_source(
    kept: Product, merged: Product, expected: Product
) -> None:
    """D3-01, D3-03, D3-04: the size never mixes a value and a unit of two products; the name's
    source carries the confirmation."""
    assert merged_fields(kept, merged) == expected


def test_the_kept_product_after_a_merge_and_nothing_else_written(
    database: ScratchDatabase,
) -> None:
    """D3-02, D3-05: K takes S's category (K had none) and keeps the rest; S's losing name is
    not written as an alias, tag or correction."""
    kept = product(
        canonical_name="Bell peppers",
        brand="Sunset",
        size_value=None,
        size_unit=None,
        category=None,
        tags=("organic",),
        field_sources={"canonical_name": PERSON, "brand": OFF, "tags": PERSON},
    )
    k = _stored(database, kept)
    s = _stored(
        database,
        product(
            canonical_name="RED PEPPERS 6PK",
            brand=None,
            size_value=None,
            size_unit=None,
            category=Category.PRODUCE,
            tags=("red",),
            field_sources={"canonical_name": TEXT, "category": LLM, "tags": LLM},
        ),
    )
    before = database.state

    merge = MergeProducts(database.uow_factory)(s, k)

    after = database.state
    expected = replace(
        kept,
        id=k,
        category=Category.PRODUCE,
        field_sources={**kept.field_sources, "category": LLM},
    )
    assert merge.kept == after.products[k] == expected
    assert (after.aliases, after.corrections) == (before.aliases, before.corrections)
    assert "RED PEPPERS 6PK" not in repr(after)


def test_a_winning_masked_name_is_copied_as_stored(database: ScratchDatabase) -> None:
    """D3-06: S's name was masked when stored; K takes it as it is."""

    def seed(uow: UnitOfWork) -> tuple[int, int]:
        merged = new_product(
            uow,
            f"Peppers {TEST_PAN}",
            None,
            EMPTY.created_at,
            field_sources={"canonical_name": PERSON},
        )
        kept = uow.products.add(_name("Kept", LLM))
        return require_id(merged.id), require_id(kept.id)

    s, k = in_unit(database.uow_factory, seed)

    MergeProducts(database.uow_factory)(s, k)

    stored = database.state.products[k]
    assert (stored.canonical_name, stored.field_sources) == (
        f"Peppers {MASKED_TEST_PAN}",
        {"canonical_name": PERSON},
    )


@pytest.mark.parametrize(
    ("pair", "error", "message"),
    [
        (("k", "k"), DomainValidationError, "cannot be merged into itself"),
        ((999, 999), DomainValidationError, "cannot be merged into itself"),
        ((999, "k"), NotFoundError, "product 999 does not exist"),
        (("s", 999), NotFoundError, "product 999 does not exist"),
    ],
)
def test_a_refused_merge_writes_nothing(
    database: ScratchDatabase,
    pair: tuple[int | str, int | str],
    error: type[Exception],
    message: str,
) -> None:
    """D5-01, D5-02: itself is checked before existence; an unknown id is named."""
    ids = {"s": _stored(database, product()), "k": _stored(database, product())}
    before, commits = database.state, database.commits
    product_id, into_id = (ids[side] if isinstance(side, str) else side for side in pair)

    with pytest.raises(error, match=message):
        MergeProducts(database.uow_factory)(product_id, into_id)

    assert (database.state, database.commits) == (before, commits)


@pytest.mark.parametrize(
    ("jobs", "refused"),
    [
        (["pending"], True),
        (["held"], True),
        (["running"], True),
        (["succeeded", "failed"], False),
    ],
)
def test_a_merge_waits_until_no_job_waits_or_runs(
    database: ScratchDatabase, jobs: list[str], refused: bool
) -> None:
    """D5-08: any receipt's job queued or running, a CLI's hold included, refuses it naming the
    receipt; a preview writes nothing and is not refused."""
    s, k = _stored(database, product()), _stored(database, product())

    def queue(uow: UnitOfWork) -> None:
        receipt_id = require_id(uow.receipts.add(receipt()).id)
        for status in jobs:
            if status == "held":
                uow.jobs.hold(receipt_id, "cli", timedelta(minutes=5))
                continue
            job = uow.jobs.enqueue(JobKind.PROCESS_RECEIPT, receipt_id, max_attempts=1)
            if status == "pending":
                continue
            claimed = uow.jobs.claim("worker", timedelta(minutes=5))
            assert claimed is not None
            assert claimed.id == job.id
            if status == "succeeded":
                uow.jobs.complete(require_id(job.id), "worker")
            elif status == "failed":
                uow.jobs.fail(require_id(job.id), "worker", "boom", retry_at=None)

    in_unit(database.uow_factory, queue)
    before = database.state

    if refused:
        with pytest.raises(ConflictError, match=re.escape(busy_message([1]))):
            MergeProducts(database.uow_factory)(s, k)
        assert MergeProducts(database.uow_factory).preview(s, k).merged.id == s
        assert database.state == before
    else:
        MergeProducts(database.uow_factory)(s, k)
        assert s not in database.state.products


def test_a_queued_enrichment_of_the_merged_product_writes_nothing(
    database: ScratchDatabase, clock: FakeClock
) -> None:
    """D5-10: a target for S picked before the merge finds no product and asks nothing."""
    k = _stored(database, _name("Kept", PERSON))
    s = _stored(database, _name("PRINTED PEPPERS", TEXT))
    MergeProducts(database.uow_factory)(s, k)
    lookup = FakeLookup(
        "off", {GTIN: LookupHit(name="Peppers", brand="Acme", quantity="500 g", categories=())}
    )
    enricher = ProductEnricher(
        database.uow_factory,
        FakeCatalog(),
        [lookup],
        reader=None,
        model="claude-haiku-5-5",
        clock=clock,
        throttle=Throttle(monotonic=lambda: 0.0, sleep=lambda _: None),
    )
    target = EnrichTarget(
        product_id=s,
        printed_text="PRINTED PEPPERS",
        store_name="COSTCO WHOLESALE #1234",
        store_key="costco",
        code=GTIN,
        gtin=GTIN,
    )
    products = database.state.products

    (queued,) = enricher.enrich_all([target])

    assert (queued.outcome, queued.error) == (EnrichOutcome.FAILED, "the product does not exist")
    assert lookup.asked == []
    assert database.state.products == products
