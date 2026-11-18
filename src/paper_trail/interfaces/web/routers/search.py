"""One search across the products bought and the receipts."""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query

from paper_trail.interfaces.web.dependencies import ContainerDep
from paper_trail.interfaces.web.errors import problem_responses
from paper_trail.interfaces.web.schemas.search import SearchOut

SEARCH_MIN_CHARS = 2
SEARCH_MAX_CHARS = 100

router = APIRouter(
    tags=["search"],
    responses=problem_responses(HTTPStatus.UNPROCESSABLE_ENTITY, HTTPStatus.INTERNAL_SERVER_ERROR),
)


@router.get("/search", operation_id="search", summary="Search products and receipts")
def search(
    container: ContainerDep,
    q: Annotated[str, Query(min_length=SEARCH_MIN_CHARS, max_length=SEARCH_MAX_CHARS)],
) -> SearchOut:
    """Products bought (name, brand, tag or category) and receipts (store name, or a line's
    printed text, description or product), every word matching as a word prefix. Search syntax
    is plain text. A query shorter than 2 or longer than 100 characters is a 422 problem."""
    return SearchOut.from_results(container.search(q))
