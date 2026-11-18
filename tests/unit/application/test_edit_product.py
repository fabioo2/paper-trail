"""A person editing a product's fields (``EditProduct``), on a scratch database.

Pinned: every given field is written and stamped ``person``, the others keep their values and
sources; free text is trimmed and masked, tags lowercased with blanks and repeats dropped; an
empty brand or size, or no tags, clears the field and its stamp; an unchanged product writes
nothing; a blank name, an unreadable size or an unknown product writes nothing; a field whose
stored value is no longer the one the person saw refuses the edit.
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from paper_trail.application.catalog import EditProduct, ProductEdit
from paper_trail.domain.enums import Category, FieldSource, Unit
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import Product
from tests.factories import MASKED_TEST_PAN, TEST_PAN, product
from tests.fakes.clock import FakeClock
from tests.storage import ScratchDatabase, in_unit, scratch_database
from tests.support import require_id

LLM, PERSON = FieldSource.LLM, FieldSource.PERSON
GUESSED = product(
    field_sources=dict.fromkeys(("canonical_name", "brand", "size", "category", "tags"), LLM)
)


@pytest.fixture
def database() -> ScratchDatabase:
    return scratch_database(FakeClock())


@pytest.fixture
def stored(database: ScratchDatabase) -> Product:
    return in_unit(database.uow_factory, lambda uow: uow.products.add(GUESSED))


def _edit(database: ScratchDatabase, item: Product, edit: ProductEdit) -> Product:
    return EditProduct(database.uow_factory)(require_id(item.id), edit)


def test_given_fields_are_written_and_stamped_person(
    database: ScratchDatabase, stored: Product
) -> None:
    edited = _edit(
        database,
        stored,
        ProductEdit(
            name=f"  Organic Eggs {TEST_PAN} ",
            size="500 g",
            category=Category.PANTRY,
            tags=(" Organic", "organic", " ", "Free-Run"),
        ),
    )

    assert edited == database.state.products[require_id(stored.id)]
    assert edited == replace(
        stored,
        canonical_name=f"Organic Eggs {MASKED_TEST_PAN}",
        size_value=Decimal(500),
        size_unit=Unit.G,
        category=Category.PANTRY,
        tags=("organic", "free-run"),
        field_sources={
            **dict.fromkeys(("canonical_name", "size", "category", "tags"), PERSON),
            "brand": LLM,
        },
    )


def test_an_empty_brand_or_size_and_no_tags_clear_the_field_and_its_stamp(
    database: ScratchDatabase, stored: Product
) -> None:
    edited = _edit(database, stored, ProductEdit(brand=" ", size="", tags=()))

    assert edited == replace(
        stored,
        brand=None,
        size_value=None,
        size_unit=None,
        tags=(),
        field_sources={"canonical_name": LLM, "category": LLM},
    )


def test_an_unchanged_product_writes_nothing(database: ScratchDatabase, stored: Product) -> None:
    edit = ProductEdit(name=stored.canonical_name)
    _edit(database, stored, edit)
    commits = database.commits

    again = _edit(database, stored, edit)

    assert database.commits == commits
    assert again.field_sources["canonical_name"] is PERSON


@pytest.mark.parametrize(
    "edit",
    [
        ProductEdit(name="  "),
        ProductEdit(name="Eggs", size="a dozen"),
        ProductEdit(size="0 g"),
        ProductEdit(tags=("sweet, salty",)),
    ],
)
def test_an_invalid_edit_writes_nothing(
    database: ScratchDatabase, stored: Product, edit: ProductEdit
) -> None:
    commits = database.commits

    with pytest.raises(DomainValidationError):
        _edit(database, stored, edit)

    assert database.commits == commits
    assert database.state.products[require_id(stored.id)] == stored


def test_an_unknown_product_is_not_found(database: ScratchDatabase, stored: Product) -> None:
    with pytest.raises(NotFoundError):
        EditProduct(database.uow_factory)(require_id(stored.id) + 1, ProductEdit(name="Eggs"))


def test_an_edit_of_values_changed_since_they_were_shown_is_refused(
    database: ScratchDatabase, stored: Product
) -> None:
    commits = database.commits
    edit = EditProduct(database.uow_factory)
    product_id = require_id(stored.id)

    with pytest.raises(ConflictError, match="changed since it was shown: brand, size"):
        edit(product_id, ProductEdit(brand=""), {"brand": "Kirkland", "size": "12 each"})
    assert database.commits == commits
    assert database.state.products[product_id] == stored

    edited = edit(product_id, ProductEdit(brand=""), {"brand": stored.brand, "size": "24 each"})

    assert edited == database.state.products[product_id]
    assert edited.brand is None
