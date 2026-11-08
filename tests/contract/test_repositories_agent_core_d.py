"""Storage port addition of agent core slice D, for the fake and SQLite (PLAN 6 enrichment).

Pinned (docs/tests/agent-core-D.md):
- ``ProductRepository.update(product: Product) -> Product`` replaces the stored product's
  ``canonical_name``, ``brand``, ``size_value``, ``size_unit``, ``category`` and ``tags`` and
  returns it as stored; ``created_at`` and the id are kept, and so are its aliases, vector and
  prices. NotFoundError for an unknown id, and nothing is changed. The full-text index follows
  the new name and tags (SQLite: the ``products_fts`` triggers), so a product can be found by a
  tag the enrichment added (what S6-AC-02 searches by later).
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from paper_trail.domain.enums import Category, Unit
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import Product
from paper_trail.ports.search import HitKind
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import at, product, product_alias
from tests.fakes.embeddings import basis
from tests.storage import in_unit
from tests.support import require_id


def _bare(uow_factory: UnitOfWorkFactory) -> Product:
    """A product known only by its name, as ``save_matches`` creates one."""
    bare = product(
        canonical_name="Kirkland Organic Eggs 24 ct",
        brand=None,
        size_value=None,
        size_unit=None,
        category=None,
        tags=(),
    )
    return in_unit(uow_factory, lambda uow: uow.products.add(bare))


def _enriched(stored: Product) -> Product:
    return replace(
        stored,
        canonical_name="Kirkland Signature Organic Eggs 24 ct",
        brand="Kirkland Signature",
        size_value=Decimal("24"),
        size_unit=Unit.EACH,
        category=Category.DAIRY_EGGS,
        tags=("eggs", "organic", "breakfast"),
        created_at=at(60),  # not stored: created_at never changes
    )


def test_update_replaces_the_enrichable_fields_and_keeps_the_rest(
    uow_factory: UnitOfWorkFactory,
) -> None:
    stored = _bare(uow_factory)
    product_id = require_id(stored.id)
    in_unit(uow_factory, lambda uow: uow.products.add_alias(product_alias(product_id)))
    in_unit(uow_factory, lambda uow: uow.vectors.upsert(product_id, basis(0)))

    returned = in_unit(uow_factory, lambda uow: uow.products.update(_enriched(stored)))

    with uow_factory(read_only=True) as uow:
        again = uow.products.get(product_id)
        aliases = uow.products.aliases(product_id)
        nearest = uow.vectors.nearest(basis(0), k=1)
    assert again == returned
    assert again == replace(_enriched(stored), created_at=stored.created_at)
    assert [alias.alias for alias in aliases] == ["ks org eggs 24ct"]
    assert [(hit.product_id, round(hit.distance, 6)) for hit in nearest] == [(product_id, 0.0)]


def test_update_of_an_unknown_product_is_not_found_and_changes_nothing(
    uow_factory: UnitOfWorkFactory,
) -> None:
    stored = _bare(uow_factory)
    ghost = replace(_enriched(stored), id=require_id(stored.id) + 100)

    with pytest.raises(NotFoundError):
        in_unit(uow_factory, lambda uow: uow.products.update(ghost))

    with uow_factory(read_only=True) as uow:
        assert uow.products.get(require_id(stored.id)) == stored
        assert uow.products.get(require_id(ghost.id)) is None


def test_the_text_index_follows_an_update(uow_factory: UnitOfWorkFactory) -> None:
    stored = _bare(uow_factory)
    with uow_factory(read_only=True) as uow:
        before = uow.text_search.search("breakfast", limit=10)

    in_unit(uow_factory, lambda uow: uow.products.update(_enriched(stored)))

    with uow_factory(read_only=True) as uow:
        after = uow.text_search.search("breakfast", limit=10)
        renamed = uow.text_search.search("signature", limit=10)
    product_hit = (HitKind.PRODUCT, require_id(stored.id))
    assert product_hit not in [(hit.kind, hit.id) for hit in before]
    assert product_hit in [(hit.kind, hit.id) for hit in after]
    assert product_hit in [(hit.kind, hit.id) for hit in renamed]
