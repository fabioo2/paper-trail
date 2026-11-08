"""The FastAPI application factory."""

from typing import Any

from fastapi import FastAPI
from fastapi.telemetry import TelemetryConfig

from paper_trail import __version__
from paper_trail.bootstrap import Container
from paper_trail.interfaces.web.errors import install_error_handlers, problem_schemas
from paper_trail.interfaces.web.middleware import RequestContextMiddleware
from paper_trail.interfaces.web.routers import health, meta, receipts
from paper_trail.interfaces.web.spa import SpaStaticFiles

API_PREFIX = "/api"

# FastAPI ships native OpenTelemetry hooks (no-op without an SDK). Paper Trail has no
# telemetry (ADR-0015), so they are switched off explicitly rather than left dormant.
NO_TELEMETRY: TelemetryConfig = {
    "tracing": False,
    "metrics": False,
    "logs": False,
    "operation_spans": False,
    "auto_configure": False,
}


class PaperTrailApi(FastAPI):
    """FastAPI whose OpenAPI document also defines the problem+json schemas it references."""

    def openapi(self) -> dict[str, Any]:
        first_build = self.openapi_schema is None
        schema = super().openapi()
        if first_build:
            schema.setdefault("components", {}).setdefault("schemas", {}).update(problem_schemas())
        return schema


def create_app(container: Container) -> FastAPI:
    """Build the API (and, when the built SPA is present, the SPA) for ``container``."""
    app = PaperTrailApi(
        title="Paper Trail",
        version=__version__,
        telemetry=NO_TELEMETRY,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=None,  # the bundled Swagger UI and ReDoc pages load scripts from a CDN
        redoc_url=None,
    )
    app.state.container = container
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    for router in (health.router, receipts.router, meta.router):
        app.include_router(router, prefix=API_PREFIX)
    if container.settings.static_dir.is_dir():
        app.mount("/", SpaStaticFiles(directory=container.settings.static_dir), name="spa")
    return app
