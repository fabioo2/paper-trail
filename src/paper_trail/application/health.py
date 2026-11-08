"""Readiness: is every component the application depends on usable?"""

from collections.abc import Sequence
from dataclasses import dataclass

from paper_trail.domain.health import ComponentHealth, HealthStatus
from paper_trail.ports.health import HealthProbe


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    components: tuple[ComponentHealth, ...]


class CheckReadiness:
    """Run every probe on each call; ready iff no component reports an error."""

    def __init__(self, probes: Sequence[HealthProbe]) -> None:
        self._probes = tuple(probes)

    def __call__(self) -> ReadinessReport:
        components = tuple(probe.check() for probe in self._probes)
        ready = all(component.status is not HealthStatus.ERROR for component in components)
        return ReadinessReport(ready=ready, components=components)
