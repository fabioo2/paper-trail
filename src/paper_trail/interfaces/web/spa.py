"""Serves the built React app from the same origin as the API (ADR-0014)."""

from http import HTTPStatus
from pathlib import PurePosixPath

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

INDEX = "index.html"
MANIFEST_MEDIA_TYPE = "application/manifest+json"
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


class SpaStaticFiles(StaticFiles):
    """Static files with client-side routing.

    - ``assets/*`` are content-hashed by Vite and cached for a year.
    - Any other existing file is served as-is and revalidated on every use.
    - A missing extension-less path outside ``api/`` is a client route: it gets ``index.html``.
    - Everything else that is missing is a 404 (a problem+json response from the app).
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != HTTPStatus.NOT_FOUND or not _is_client_route(path):
                raise
            path = INDEX
            response = await super().get_response(path, scope)
        _set_cache_headers(PurePosixPath(path), response)
        return response


def _is_client_route(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return not (parts and parts[0] in {"api", "assets"}) and not PurePosixPath(path).suffix


def _set_cache_headers(path: PurePosixPath, response: Response) -> None:
    if path.parts and path.parts[0] == "assets":
        response.headers["Cache-Control"] = IMMUTABLE
        return
    response.headers["Cache-Control"] = REVALIDATE
    if path.suffix == ".webmanifest":
        response.headers["Content-Type"] = MANIFEST_MEDIA_TYPE
