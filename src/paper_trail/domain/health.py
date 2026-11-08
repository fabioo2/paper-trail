"""Health of the application's components, as reported by readiness probes."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class HealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """The state of one component, e.g. the database."""

    name: str
    status: HealthStatus
    detail: str | None = None
    data: Mapping[str, str] = field(default_factory=dict)
