"""A printed department heading names a new product's category, as printed text, and never
beats a stronger source (claude-extraction plan EC section, E1; CE-TC-016).

The hint is the store's map (``DEPARTMENT_CATEGORIES``, Metro only at first, CE-TC-015 owns the
map): a new product gets it stamped ``receipt_text`` (unconfirmed, like its printed name); an
existing product the line links to keeps its category and sources whatever the heading says; an
unknown heading leaves the category empty and unstamped.
"""

import pytest

from paper_trail.domain.enums import AliasSource, Category, FieldSource, LineKind
from paper_trail.domain.models import Product
from tests.agent_harness import Harness, build_harness
from tests.extractions import METRO, METRO_KEY, item, metro_answer, receipt_answer
from tests.factories import product, product_alias
from tests.fakes.reader import ScriptedReader
from tests.storage import in_unit
from tests.support import require_id

RECEIPT_TEXT = FieldSource.RECEIPT_TEXT


def _products_by_item(harness: Harness, receipt_id: int) -> list[Product]:
    return [
        harness.product(require_id(line.product_id))
        for line in harness.receipt(receipt_id).items
        if line.kind is LineKind.ITEM
    ]


def test_ce_tc_016_new_metro_products_take_their_departments_category_as_printed_text() -> None:
    harness = build_harness(ScriptedReader(metro_answer()))

    receipt_id = harness.process_image("metro").receipt_id

    assert [
        (found.canonical_name, found.category, dict(found.field_sources))
        for found in _products_by_item(harness, receipt_id)
    ] == [
        ("BANANAS", Category.PRODUCE, {"canonical_name": RECEIPT_TEXT, "category": RECEIPT_TEXT}),
        (
            "ROMAINE HEARTS",
            Category.PRODUCE,
            {"canonical_name": RECEIPT_TEXT, "category": RECEIPT_TEXT},
        ),
        ("WHOLE GRAIN CRACKERS", None, {"canonical_name": RECEIPT_TEXT}),
        (
            "TOMATOES ON VINE",
            Category.PRODUCE,
            {"canonical_name": RECEIPT_TEXT, "category": RECEIPT_TEXT},
        ),
        (
            "DAILY SHAMPOO",
            Category.PERSONAL_CARE,
            {"canonical_name": RECEIPT_TEXT, "category": RECEIPT_TEXT},
        ),
        ("PASTA SAUCE", None, {"canonical_name": RECEIPT_TEXT}),
        (
            "TOOTHPASTE 4PK",
            Category.PERSONAL_CARE,
            {"canonical_name": RECEIPT_TEXT, "category": RECEIPT_TEXT},
        ),
    ]


@pytest.mark.parametrize(
    "source",
    [FieldSource.PERSON, FieldSource.PLU, FieldSource.OFF, FieldSource.LEGACY_MODEL],
)
def test_ce_tc_016_a_heading_never_changes_an_existing_products_category(
    source: FieldSource,
) -> None:
    harness = build_harness(ScriptedReader(metro_answer()))
    stored = product(
        canonical_name="Bananas",
        category=Category.SNACKS_CANDY,
        field_sources={"canonical_name": source, "category": source},
    )
    bananas = in_unit(harness.uow_factory, lambda uow: uow.products.add(stored))
    known = require_id(bananas.id)
    alias = product_alias(known, alias="bananas", store_key=METRO_KEY, source=AliasSource.MATCHED)
    in_unit(harness.uow_factory, lambda uow: uow.products.add_alias(alias))

    receipt_id = harness.process_image("metro").receipt_id

    assert harness.line(receipt_id, 0).product_id == known
    assert harness.product(known) == bananas


def test_ce_tc_016_an_unknown_heading_leaves_the_category_alone() -> None:
    reading = receipt_answer(
        [item("GARDEN GNOME", "9.99", department="SEASONAL XYZ")],
        total="9.99",
        store_name=METRO,
        purchased_on="2026-11-03",
    )
    harness = build_harness(ScriptedReader(reading))

    receipt_id = harness.process_image("metro").receipt_id

    (created,) = _products_by_item(harness, receipt_id)
    assert (created.category, dict(created.field_sources)) == (
        None,
        {"canonical_name": RECEIPT_TEXT},
    )
