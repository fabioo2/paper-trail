"""Health probe port, implemented once per external dependency."""

from typing import Protocol

from paper_trail.domain.health import ComponentHealth


class HealthProbe(Protocol):
    @property
    def name(self) -> str:
        """Stable component name used in the readiness report."""
        ...

    def check(self) -> ComponentHealth:
        """Probe the component. Must not raise: failures are reported as HealthStatus.ERROR."""
        ...
