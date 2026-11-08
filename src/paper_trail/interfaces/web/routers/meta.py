"""Reference data: static, so it needs no database."""

from http import HTTPStatus

from fastapi import APIRouter

from paper_trail.interfaces.web.errors import problem_responses
from paper_trail.interfaces.web.schemas.meta import CategoryOut, categories

router = APIRouter(tags=["meta"], responses=problem_responses(HTTPStatus.INTERNAL_SERVER_ERROR))


@router.get("/categories", operation_id="listCategories", summary="List categories")
async def list_categories() -> list[CategoryOut]:
    """Every product category with its display label, in a stable order."""
    return categories()
