"""Products: one product with where each of its fields came from."""

from http import HTTPStatus

from fastapi import APIRouter

from paper_trail.interfaces.web.dependencies import ContainerDep
from paper_trail.interfaces.web.errors import problem_responses
from paper_trail.interfaces.web.schemas.products import ProductOut

router = APIRouter(
    prefix="/products",
    tags=["products"],
    responses=problem_responses(HTTPStatus.UNPROCESSABLE_ENTITY, HTTPStatus.INTERNAL_SERVER_ERROR),
)


@router.get(
    "/{product_id}",
    operation_id="getProduct",
    summary="Get a product",
    responses=problem_responses(HTTPStatus.NOT_FOUND),
)
def get_product(container: ContainerDep, product_id: int) -> ProductOut:
    """One product with its field sources; an unknown id is a 404 problem."""
    return ProductOut.from_domain(container.get_product(product_id))
