"""Liveness (no I/O) and readiness (every dependency) endpoints."""

from http import HTTPStatus

from fastapi import APIRouter, Response

from paper_trail import __version__
from paper_trail.interfaces.web.dependencies import ContainerDep
from paper_trail.interfaces.web.errors import problem_responses
from paper_trail.interfaces.web.schemas.health import HealthOut, ReadinessOut

router = APIRouter(
    prefix="/health",
    tags=["health"],
    responses=problem_responses(HTTPStatus.INTERNAL_SERVER_ERROR),
)


@router.get("", operation_id="getHealth", summary="Liveness")
async def get_health(container: ContainerDep) -> HealthOut:
    """The process is up. Touches no database or other dependency."""
    return HealthOut(status="ok", version=__version__, build_sha=container.settings.build_sha)


@router.get(
    "/ready",
    operation_id="getReadiness",
    summary="Readiness",
    responses={
        HTTPStatus.SERVICE_UNAVAILABLE.value: {
            "model": ReadinessOut,
            "description": "A component is not ready; the body says which and why.",
        }
    },
)
def get_readiness(container: ContainerDep, response: Response) -> ReadinessOut:
    """Every component is usable. Answers 503 with the same body when one is not."""
    report = container.check_readiness()
    if not report.ready:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
    return ReadinessOut.from_report(report)
