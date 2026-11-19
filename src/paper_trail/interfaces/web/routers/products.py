"""Products: the products bought, one product with where each of its fields came from and its
purchases, and a person's edit or confirmation of one."""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query

from paper_trail.interfaces.web.dependencies import ContainerDep
from paper_trail.interfaces.web.errors import problem_responses
from paper_trail.interfaces.web.schemas.products import (
    ProductConfirmIn,
    ProductMergeIn,
    ProductOut,
    ProductPurchaseOut,
    ProductSummaryOut,
    ProductToCheckOut,
    ProductUpdateIn,
)

router = APIRouter(
    prefix="/products",
    tags=["products"],
    responses=problem_responses(HTTPStatus.UNPROCESSABLE_ENTITY, HTTPStatus.INTERNAL_SERVER_ERROR),
)


@router.get("", operation_id="listProducts", summary="List products")
def list_products(container: ContainerDep) -> list[ProductSummaryOut]:
    """Every product with at least one receipt line, by name, each with how often it was
    bought and its newest purchase."""
    return [ProductSummaryOut.from_summary(summary) for summary in container.list_products()]


@router.get("/to-check", operation_id="listProductsToCheck", summary="List products to check")
def list_products_to_check(container: ContainerDep) -> list[ProductToCheckOut]:
    """Every bought product nobody has confirmed yet, most bought first, then the newest
    purchase, then the product id; each with its earliest receipt line as printed."""
    return [ProductToCheckOut.from_row(row) for row in container.catalog_todo()]


@router.get(
    "/{product_id}",
    operation_id="getProduct",
    summary="Get a product",
    responses=problem_responses(HTTPStatus.NOT_FOUND),
)
def get_product(container: ContainerDep, product_id: int) -> ProductOut:
    """One product with its field sources; an unknown id is a 404 problem."""
    return ProductOut.from_domain(container.get_product(product_id))


@router.get(
    "/{product_id}/purchases",
    operation_id="listProductPurchases",
    summary="List a product's purchases",
    responses=problem_responses(HTTPStatus.NOT_FOUND),
)
def list_product_purchases(container: ContainerDep, product_id: int) -> list[ProductPurchaseOut]:
    """Every receipt line that bought the product, newest first (undated last), paid after
    its discount; an unknown id is a 404 problem."""
    return [
        ProductPurchaseOut.from_domain(purchase)
        for purchase in container.list_product_purchases(product_id)
    ]


@router.get(
    "/{product_id}/merge-candidates",
    operation_id="listMergeCandidates",
    summary="List products that may be the same one",
    responses=problem_responses(HTTPStatus.NOT_FOUND),
)
def list_merge_candidates(container: ContainerDep, product_id: int) -> list[ProductSummaryOut]:
    """Bought products that may be the same as this one: those sharing one of its codes first,
    then those sharing a word of its name, best match first (at most 10); never the product
    itself. An unknown id is a 404 problem."""
    return [
        ProductSummaryOut.from_summary(summary)
        for summary in container.merge_candidates(product_id)
    ]


@router.get(
    "/{product_id}/merge-preview",
    operation_id="previewMerge",
    summary="Preview merging a product into the same one",
    responses=problem_responses(HTTPStatus.NOT_FOUND),
)
def preview_merge(
    container: ContainerDep, product_id: int, into_id: Annotated[int, Query(ge=1)]
) -> ProductOut:
    """The product ``into_id`` as merging this one into it would leave it (its name, brand,
    size, category and tags, each the better-sourced of the two); nothing is written. Merging
    a product into itself is a 422 problem, an unknown id a 404 problem."""
    return ProductOut.from_domain(container.merge_products.preview(product_id, into_id).kept)


@router.patch(
    "/{product_id}",
    operation_id="updateProduct",
    summary="Edit a product",
    responses=problem_responses(HTTPStatus.NOT_FOUND, HTTPStatus.CONFLICT),
)
def update_product(container: ContainerDep, product_id: int, body: ProductUpdateIn) -> ProductOut:
    """Write the fields sent and stamp each ``person``; the others are kept. A stored value
    that differs from its ``expected`` one is a 409 problem (nothing written); a blank name or
    an unreadable size is a 422 problem, an unknown id a 404 problem."""
    return ProductOut.from_domain(container.edit_product(product_id, body.to_edit(), body.expected))


@router.post(
    "/{product_id}/confirm",
    operation_id="confirmProduct",
    summary="Confirm a product",
    responses=problem_responses(HTTPStatus.NOT_FOUND, HTTPStatus.CONFLICT),
)
def confirm_product(
    container: ContainerDep, product_id: int, body: ProductConfirmIn | None = None
) -> ProductOut:
    """Accept the product's current values as the person's (every filled field stamped
    ``person``, or only the filled ``fields`` sent, the others' sources kept). A stored value
    that differs from its ``expected`` one is a 409 problem (nothing written); an unknown id
    a 404 problem."""
    if body is None:
        return ProductOut.from_domain(container.confirm_product(product_id))
    return ProductOut.from_domain(container.confirm_product(product_id, body.fields, body.expected))


@router.post(
    "/{product_id}/merge",
    operation_id="mergeProduct",
    summary="Merge a product into the same one",
    responses=problem_responses(HTTPStatus.NOT_FOUND, HTTPStatus.CONFLICT),
)
def merge_product(container: ContainerDep, product_id: int, body: ProductMergeIn) -> ProductOut:
    """The product is the same as ``into_id``: its purchases, prices, codes, aliases and
    corrections move to that one, which takes each of its better-sourced fields, and it is
    deleted. Returns the kept product. Only a backup undoes it. Merging a product into itself
    is a 422 problem, an unknown id a 404 problem; while a receipt is being read, a 409
    problem (nothing written)."""
    return ProductOut.from_domain(container.merge_products(product_id, body.into_id).kept)
