"""Products: the products bought, one product with where each of its fields came from and its
purchases, and a person's edit or confirmation of one."""

from http import HTTPStatus

from fastapi import APIRouter

from paper_trail.interfaces.web.dependencies import ContainerDep
from paper_trail.interfaces.web.errors import problem_responses
from paper_trail.interfaces.web.schemas.products import (
    ProductConfirmIn,
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


@router.patch(
    "/{product_id}",
    operation_id="updateProduct",
    summary="Edit a product",
    responses=problem_responses(HTTPStatus.NOT_FOUND),
)
def update_product(container: ContainerDep, product_id: int, body: ProductUpdateIn) -> ProductOut:
    """Write the fields sent and stamp each ``person``; the others are kept. A blank name or
    an unreadable size is a 422 problem, an unknown id a 404 problem."""
    return ProductOut.from_domain(container.edit_product(product_id, body.to_edit()))


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
