"""Liveness and readiness response bodies."""

from typing import Literal

from pydantic import BaseModel

from paper_trail.application.health import ReadinessReport
from paper_trail.domain.health import HealthStatus


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str
    build_sha: str | None


class ComponentOut(BaseModel):
    name: str
    status: HealthStatus
    detail: str | None
    data: dict[str, str]


class ReadinessOut(BaseModel):
    status: Literal["ok", "error"]
    components: list[ComponentOut]

    @classmethod
    def from_report(cls, report: ReadinessReport) -> "ReadinessOut":
        return cls(
            status="ok" if report.ready else "error",
            components=[
                ComponentOut(
                    name=component.name,
                    status=component.status,
                    detail=component.detail,
                    data=dict(component.data),
                )
                for component in report.components
            ],
        )
