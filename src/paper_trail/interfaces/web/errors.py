"""RFC 9457 problem details for every error the API returns (PLAN 1.5)."""

from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from pydantic.json_schema import models_json_schema
from starlette.exceptions import HTTPException as StarletteHTTPException

from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError

PROBLEM_MEDIA_TYPE = "application/problem+json"
_SCHEMA_REF_TEMPLATE = "#/components/schemas/{model}"

# Problem types for statuses with Paper Trail-specific meaning; others use "about:blank".
_PROBLEM_TYPES: Mapping[int, str] = {
    404: "/problems/not-found",
    409: "/problems/conflict",
    422: "/problems/validation",
    500: "/problems/internal",
    503: "/problems/not-ready",
}


class FieldError(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class ProblemDetail(BaseModel):
    # Every key is always serialized, so the generated TS type marks them all as present.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[FieldError] | None = None


def problem_response(
    status: int,
    *,
    detail: str | None = None,
    instance: str | None = None,
    errors: list[FieldError] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    problem = ProblemDetail(
        type=_PROBLEM_TYPES.get(status, "about:blank"),
        title=HTTPStatus(status).phrase,
        status=status,
        detail=detail,
        instance=instance,
        errors=errors,
    )
    return JSONResponse(
        problem.model_dump(mode="json"),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def internal_error_response() -> JSONResponse:
    """A 500 that reveals nothing about the failure; the traceback is logged instead."""
    return problem_response(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        detail="An unexpected error occurred. It has been logged with this request's id.",
    )


def problem_responses(*statuses: HTTPStatus) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses=`` entries declaring problem+json bodies."""
    return {
        status.value: {
            "description": status.phrase,
            "content": {
                PROBLEM_MEDIA_TYPE: {
                    "schema": {"$ref": _SCHEMA_REF_TEMPLATE.format(model=ProblemDetail.__name__)}
                }
            },
        }
        for status in statuses
    }


def problem_schemas() -> dict[str, Any]:
    """JSON schemas of ProblemDetail and its parts, for ``components.schemas``."""
    _, definitions = models_json_schema(
        [(ProblemDetail, "serialization")], ref_template=_SCHEMA_REF_TEMPLATE
    )
    schemas: dict[str, Any] = definitions["$defs"]
    return schemas


def install_error_handlers(app: FastAPI) -> None:
    domain_errors = {NotFoundError: 404, ConflictError: 409, DomainValidationError: 422}
    for error_class, status in domain_errors.items():
        app.add_exception_handler(error_class, _domain_error_handler(status))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        default_detail = HTTPStatus(exc.status_code).phrase
        return problem_response(
            exc.status_code,
            detail=None if exc.detail == default_detail else str(exc.detail),
            instance=request.url.path,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        errors = [
            FieldError(loc=list(error["loc"]), msg=error["msg"], type=error["type"])
            for error in jsonable_encoder(exc.errors())
        ]
        return problem_response(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="The request is invalid.",
            instance=request.url.path,
            errors=errors,
        )


def _domain_error_handler(status: int) -> Callable[[Request, Exception], Awaitable[Response]]:
    async def handle(request: Request, exc: Exception) -> Response:
        return problem_response(status, detail=str(exc), instance=request.url.path)

    return handle
