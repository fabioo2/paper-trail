"""Products: the products bought, one product with where each of its fields came from, and a
person's edit or confirmation of one."""

from http import HTTPStatus

from fastapi import APIRouter

from paper_trail.interfaces.web.dependencies import ContainerDep
from paper_trail.interfaces.web.errors import problem_responses
from paper_trail.interfaces.web.schemas.products import (
    ProductOut,
    ProductSummaryOut,
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


@router.get(
    "/{product_id}",
    operation_id="getProduct",
    summary="Get a product",
    responses=problem_responses(HTTPStatus.NOT_FOUND),
)
def get_product(container: ContainerDep, product_id: int) -> ProductOut:
    """One product with its field sources; an unknown id is a 404 problem."""
    return ProductOut.from_domain(container.get_product(product_id))


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
    responses=problem_responses(HTTPStatus.NOT_FOUND),
)
def confirm_product(container: ContainerDep, product_id: int) -> ProductOut:
    """Accept the product's current values as the person's (every filled field stamped
    ``person``); an unknown id is a 404 problem."""
    return ProductOut.from_domain(container.confirm_product(product_id))
