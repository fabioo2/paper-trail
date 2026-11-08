"""ProductRepository contract (S1-AC-10, S1-AC-12), for the fake and the SQLite adapter.

Pinned (docs/tests/stage1.md): aliases are matched exactly as stored (callers pass
``normalize_alias`` output); ``store_key=None`` matches only global aliases; on an existing
``(alias, store_key)`` a USER_CORRECTION is never replaced by another source, every other
incoming source replaces the row in place (same id); ``aliases()`` is ordered by id.
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from paper_trail.domain.enums import AliasSource, Unit
from paper_trail.domain.models import Product, ProductAlias
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import at, product, product_alias
from tests.storage import in_unit
from tests.support import require_id

ALIAS = "ks org eggs 24ct"


def _add_product(uow_factory: UnitOfWorkFactory, value: Product | None = None) -> Product:
    return in_unit(uow_factory, lambda uow: uow.products.add(value or product()))


def _add_alias(uow_factory: UnitOfWorkFactory, alias: ProductAlias) -> ProductAlias:
    return in_unit(uow_factory, lambda uow: uow.products.add_alias(alias))


def _alias_for(saved: Product, **overrides: object) -> ProductAlias:
    return product_alias(require_id(saved.id), **overrides)


def _get(uow_factory: UnitOfWorkFactory, saved: Product) -> Product | None:
    with uow_factory() as uow:
        return uow.products.get(require_id(saved.id))


def _find(uow_factory: UnitOfWorkFactory, alias: str, store_key: str | None) -> Product | None:
    with uow_factory() as uow:
        return uow.products.find_by_alias(alias, store_key)


def _aliases(uow_factory: UnitOfWorkFactory, saved: Product) -> tuple[ProductAlias, ...]:
    with uow_factory() as uow:
        return uow.products.aliases(require_id(saved.id))


def test_s1_ac_10_add_then_get_round_trips(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: Decimal size, enums and tags survive the round trip."""
    original = product(size_value=Decimal("1.36"), size_unit=Unit.KG, tags=("organic", "bulk"))

    saved = _add_product(uow_factory, original)
    loaded = _get(uow_factory, saved)

    assert saved.id is not None
    assert loaded == saved == replace(original, id=saved.id)
    assert loaded is not None
    assert str(loaded.size_value) == "1.36"
    assert loaded.tags == ("organic", "bulk")


def test_s1_ac_10_product_optional_fields_round_trip(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: a product with only a name keeps its None fields and empty tags."""
    original = product(brand=None, size_value=None, size_unit=None, category=None, tags=())

    saved = _add_product(uow_factory, original)

    assert _get(uow_factory, saved) == replace(original, id=saved.id)


def test_s1_ac_10_get_missing_product_is_none(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: get() of an unknown id is None."""
    with uow_factory() as uow:
        assert uow.products.get(12345) is None


def test_s1_ac_10_add_alias_assigns_an_id(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: add_alias returns the stored alias with an id."""
    saved = _add_product(uow_factory)

    alias = _add_alias(uow_factory, _alias_for(saved))

    assert alias.id is not None
    assert alias == replace(_alias_for(saved), id=alias.id)
    assert _aliases(uow_factory, saved) == (alias,)


def test_s1_ac_12_find_by_alias_exact_match(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-12: the alias text must match exactly; other text finds nothing."""
    saved = _add_product(uow_factory)
    _add_alias(uow_factory, _alias_for(saved, store_key=None))

    assert _find(uow_factory, ALIAS, None) == saved
    assert _find(uow_factory, "ks org eggs", None) is None
    assert _find(uow_factory, "KS ORG EGGS 24CT", None) is None


def test_s1_ac_12_store_specific_alias_wins_over_global(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-12: the store's own alias is preferred; other stores fall back to the global one."""
    everywhere = _add_product(uow_factory, product(canonical_name="Organic eggs (generic)"))
    at_costco = _add_product(uow_factory, product(canonical_name="Kirkland organic eggs"))
    _add_alias(uow_factory, _alias_for(everywhere, store_key=None))
    _add_alias(uow_factory, _alias_for(at_costco, store_key="costco"))

    assert _find(uow_factory, ALIAS, "costco") == at_costco
    assert _find(uow_factory, ALIAS, "walmart") == everywhere
    assert _find(uow_factory, ALIAS, None) == everywhere


def test_s1_ac_12_store_alias_is_not_global(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-12: a store-specific alias does not match other stores or the global lookup."""
    saved = _add_product(uow_factory)
    _add_alias(uow_factory, _alias_for(saved, store_key="costco"))

    assert _find(uow_factory, ALIAS, "costco") == saved
    assert _find(uow_factory, ALIAS, "walmart") is None
    assert _find(uow_factory, ALIAS, None) is None


@pytest.mark.parametrize("store_key", ["costco", None])
@pytest.mark.parametrize("weaker", [AliasSource.IMPORT, AliasSource.MATCHED, AliasSource.EXTRACTED])
def test_s1_ac_12_user_correction_is_kept_over_machine_sources(
    uow_factory: UnitOfWorkFactory, store_key: str | None, weaker: AliasSource
) -> None:
    """S1-AC-12: an existing USER_CORRECTION alias survives a later IMPORT/MATCHED/EXTRACTED."""
    corrected = _add_product(uow_factory, product(canonical_name="Corrected"))
    guessed = _add_product(uow_factory, product(canonical_name="Guessed"))
    kept = _add_alias(
        uow_factory,
        _alias_for(corrected, store_key=store_key, source=AliasSource.USER_CORRECTION),
    )

    result = _add_alias(
        uow_factory, _alias_for(guessed, store_key=store_key, source=weaker, created_at=at(10))
    )

    assert result == kept
    assert _find(uow_factory, ALIAS, store_key) == corrected
    assert _aliases(uow_factory, guessed) == ()


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (AliasSource.IMPORT, AliasSource.MATCHED),
        (AliasSource.MATCHED, AliasSource.IMPORT),
        (AliasSource.EXTRACTED, AliasSource.USER_CORRECTION),
        (AliasSource.IMPORT, AliasSource.USER_CORRECTION),
        (AliasSource.USER_CORRECTION, AliasSource.USER_CORRECTION),
    ],
)
def test_s1_ac_12_newer_alias_replaces_a_lower_or_equal_priority_one(
    uow_factory: UnitOfWorkFactory, first: AliasSource, second: AliasSource
) -> None:
    """S1-AC-12: a newer alias of equal or higher priority replaces the row in place."""
    old = _add_product(uow_factory, product(canonical_name="Old"))
    new = _add_product(uow_factory, product(canonical_name="New"))
    original = _add_alias(uow_factory, _alias_for(old, source=first))

    replaced = _add_alias(uow_factory, _alias_for(new, source=second, created_at=at(10)))

    assert replaced.id == original.id
    assert (replaced.product_id, replaced.source, replaced.created_at) == (new.id, second, at(10))
    assert _find(uow_factory, ALIAS, "costco") == new
    assert _aliases(uow_factory, new) == (replaced,)
    assert _aliases(uow_factory, old) == ()


def test_s1_ac_12_global_and_per_store_aliases_are_separate_keys(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """S1-AC-12: (alias, store_key) is the key, and a NULL store_key is one key like any other."""
    saved = _add_product(uow_factory)

    global_alias = _add_alias(uow_factory, _alias_for(saved, store_key=None))
    again = _add_alias(uow_factory, _alias_for(saved, store_key=None, created_at=at(1)))
    store_alias = _add_alias(uow_factory, _alias_for(saved, store_key="costco"))

    assert again.id == global_alias.id
    assert store_alias.id != global_alias.id
    assert [alias.store_key for alias in _aliases(uow_factory, saved)] == [None, "costco"]


def test_s1_ac_10_aliases_lists_only_that_product_in_id_order(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """S1-AC-10: aliases(product_id) returns that product's aliases ordered by id."""
    eggs = _add_product(uow_factory)
    milk = _add_product(uow_factory, product(canonical_name="Milk"))
    first = _add_alias(uow_factory, _alias_for(eggs, alias="eggs"))
    _add_alias(uow_factory, _alias_for(milk, alias="milk"))
    second = _add_alias(uow_factory, _alias_for(eggs, alias="organic eggs"))

    assert _aliases(uow_factory, eggs) == (first, second)
