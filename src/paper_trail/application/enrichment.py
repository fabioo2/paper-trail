"""Saving what the enrichment agent says a new product is (PLAN 6, agent core slice D).

The supervisor's ``save_enrichment`` tool writes it. A person's category for the line the
product came from wins over the model's (S4-AC-05): the newest CATEGORY correction for exactly
that alias and store. The product's vector is replaced by the embedding of its enriched name
and tags, computed before the write and outside any transaction (a slow or absent embedding
host must not hold the write lock); then the product and its vector are written in one unit of
work. Saving the same enrichment twice changes nothing more (a resumed run repeats it).
"""

from dataclasses import dataclass, replace
from typing import Literal

from paper_trail.application.payloads import ProductEnrichment
from paper_trail.domain.enums import Category, CorrectionKind
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import Product, require_id
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.ports.embeddings import EmbeddingPort
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory

type CategorySource = Literal["model", "memory"]


@dataclass(frozen=True, slots=True)
class EnrichmentSaved:
    product: Product  # as stored
    category_source: CategorySource  # who chose the category: the model or a person


def save_enrichment(  # noqa: PLR0913 - the product, where it was bought, and the answer
    uow_factory: UnitOfWorkFactory,
    embeddings: EmbeddingPort,
    product_id: int,
    *,
    alias: str,
    store_key: str | None,
    enrichment: ProductEnrichment,
) -> EnrichmentSaved:
    """Replace the product's descriptive fields and its vector. ``alias`` is the raw text of
    the line it was created from (normalized here). NotFoundError for an unknown product; a
    failed embedding writes nothing."""
    with uow_factory(read_only=True) as uow:
        current = _existing(uow, product_id)
        remembered = _remembered_category(uow, normalize_alias(alias), store_key)
    category = enrichment.category if remembered is None else remembered
    enriched = _masked(enrichment.applied_to(current, category))
    (vector,) = embeddings.embed(
        [normalize_alias(" ".join([enriched.canonical_name, *enriched.tags]))]
    )
    with uow_factory() as uow:
        stored = uow.products.update(enriched)
        uow.vectors.upsert(require_id(stored.id), vector)
        uow.commit()
    return EnrichmentSaved(stored, "model" if remembered is None else "memory")


def _existing(uow: UnitOfWork, product_id: int) -> Product:
    product = uow.products.get(product_id)
    if product is None:
        msg = f"product {product_id} does not exist"
        raise NotFoundError(msg)
    return product


def _remembered_category(uow: UnitOfWork, alias: str, store_key: str | None) -> Category | None:
    correction = uow.corrections.latest(CorrectionKind.CATEGORY, alias, store_key)
    if correction is None or correction.new_value not in Category:
        return None
    return Category(correction.new_value)


def _masked(product: Product) -> Product:
    """Card numbers masked in the free text the model wrote, like everywhere else."""
    return replace(
        product,
        canonical_name=mask_pans(product.canonical_name).text,
        brand=None if product.brand is None else mask_pans(product.brand).text,
        tags=tuple(mask_pans(tag).text for tag in product.tags),
    )
