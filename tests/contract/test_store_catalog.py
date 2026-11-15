"""``StoreCatalog``: the codes that are already ours (``product_codes``) as a catalog
(barcode-lookup PR 1a, R6, A5, A6 and the owner answer on global codes; PC-TC-34).

Runs over the fake and the SQLite unit of work. A hit is source ``store_catalog`` with the
product's id, fields and real ``field_sources``; ``key`` is the identity that hit. Barcodes
(``gtin``) and PLUs are stored with ``store_key = ''`` and found from every store whose profile
reads them as such; store and in-store codes belong to their store. An organic PLU (``93101``)
and its conventional code (``3101``) are different identities.
"""

from collections.abc import Sequence
from decimal import Decimal

import pytest

from paper_trail.adapters.sqlite.store_catalog import StoreCatalog
from paper_trail.domain.enums import Category, CodeScheme, CodeSource, FieldSource, Unit
from paper_trail.domain.models import ProductCode
from paper_trail.domain.rules.item_codes import parse_item_code
from paper_trail.ports.catalog import CatalogProduct
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.extractions import C1, L1, L3, L4, L5, LOBLAWS_KEY
from tests.factories import CREATED_AT, product
from tests.storage import in_unit
from tests.support import require_id

CHOC = product(
    canonical_name="Choc Pistachio Bars",
    brand="Kirkland Signature",
    size_value=Decimal("1.2"),
    size_unit=Unit.KG,
    category=Category.SNACKS_CANDY,
    tags=("bars",),
    field_sources={"canonical_name": FieldSource.PERSON},
)


def _seed(factory: UnitOfWorkFactory, codes: Sequence[tuple[CodeScheme, str, str]]) -> list[int]:
    """One product per code (named after it), the code attached; returns the product ids."""

    def add(uow: UnitOfWork) -> list[int]:
        ids = []
        for scheme, store_key, code in codes:
            product_id = require_id(uow.products.add(product(canonical_name=f"P {code}")).id)
            uow.product_codes.upsert(
                ProductCode(
                    None,
                    product_id,
                    scheme,
                    store_key,
                    code,
                    CodeSource.MANUAL,
                    CREATED_AT,
                    CREATED_AT,
                ),
                replace=True,
            )
            ids.append(product_id)
        return ids

    return in_unit(factory, add)


def _lookup(factory: UnitOfWorkFactory, raw_text: str, store_key: str) -> CatalogProduct | None:
    code = parse_item_code(raw_text, store_key)
    assert code is not None
    return StoreCatalog(factory).lookup(code, store_key)


def _hit_id(found: CatalogProduct | None) -> int | None:
    return None if found is None else found.product_id


def test_a_stored_store_code_is_our_product_with_its_field_sources(
    uow_factory: UnitOfWorkFactory,
) -> None:
    def add(uow: UnitOfWork) -> int:
        product_id = require_id(uow.products.add(CHOC).id)
        uow.product_codes.upsert(
            ProductCode(
                None,
                product_id,
                CodeScheme.STORE,
                "costco",
                "1362394",
                CodeSource.MANUAL,
                CREATED_AT,
                CREATED_AT,
            ),
            replace=True,
        )
        return product_id

    product_id = in_unit(uow_factory, add)

    assert _lookup(uow_factory, C1, "costco") == CatalogProduct(
        source="store_catalog",
        key="1362394",
        product_id=product_id,
        name="Choc Pistachio Bars",
        brand="Kirkland Signature",
        size_value=Decimal("1.2"),
        size_unit=Unit.KG,
        category=Category.SNACKS_CANDY,
        tags=("bars",),
        field_sources={"canonical_name": FieldSource.PERSON},
    )


@pytest.mark.parametrize(
    ("stored", "raw_text", "store_key", "hit"),
    [
        ((CodeScheme.STORE, "costco", "1362394"), "1362394 X", LOBLAWS_KEY, False),
        ((CodeScheme.STORE, "costco", "1362394"), "1362394 X", "walmart", False),
        ((CodeScheme.STORE, LOBLAWS_KEY, "2100580"), L5, LOBLAWS_KEY, True),
        ((CodeScheme.STORE, LOBLAWS_KEY, "2100580"), L5, "walmart", False),
        ((CodeScheme.GTIN, "", "0060383064877"), L1, LOBLAWS_KEY, True),
        ((CodeScheme.GTIN, "", "0060383064877"), L1, "walmart", True),
        ((CodeScheme.PLU, "", "3101"), L3, LOBLAWS_KEY, True),
        ((CodeScheme.PLU, "", "3101"), L3, "walmart", True),
        ((CodeScheme.PLU, "", "3101"), "3101 X", "costco", False),
        ((CodeScheme.PLU, "", "93101"), L3, LOBLAWS_KEY, False),
        ((CodeScheme.PLU, "", "3101"), L4, LOBLAWS_KEY, False),
        ((CodeScheme.PLU, "", "93101"), L4, LOBLAWS_KEY, True),
    ],
    ids=[
        "store-code-not-at-loblaws",
        "store-code-not-at-walmart",
        "in-store-code-at-its-store",
        "in-store-code-not-at-walmart",
        "gtin-at-loblaws",
        "gtin-at-walmart",
        "plu-at-loblaws",
        "plu-at-walmart",
        "plu-digits-are-a-store-item-at-costco",
        "organic-identity-not-found-by-conventional",
        "conventional-identity-not-found-by-organic",
        "organic-identity-found-by-organic",
    ],
)
def test_where_a_stored_code_is_found(
    uow_factory: UnitOfWorkFactory,
    stored: tuple[CodeScheme, str, str],
    raw_text: str,
    store_key: str,
    hit: bool,
) -> None:
    (product_id,) = _seed(uow_factory, [stored])

    found = _lookup(uow_factory, raw_text, store_key)

    assert _hit_id(found) == (product_id if hit else None)


def test_a_twelve_digit_code_hits_by_its_second_key(uow_factory: UnitOfWorkFactory) -> None:
    (second,) = _seed(uow_factory, [(CodeScheme.GTIN, "", "0360002914522")])

    found = _lookup(uow_factory, "036000291452 X", LOBLAWS_KEY)

    assert (_hit_id(found), found and found.key) == (second, "0360002914522")


def test_a_twelve_digit_code_prefers_its_first_key(uow_factory: UnitOfWorkFactory) -> None:
    first, _ = _seed(
        uow_factory,
        [(CodeScheme.GTIN, "", "0036000291452"), (CodeScheme.GTIN, "", "0360002914522")],
    )

    found = _lookup(uow_factory, "036000291452 X", LOBLAWS_KEY)

    assert (_hit_id(found), found and found.key) == (first, "0036000291452")
