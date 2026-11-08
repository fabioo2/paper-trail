"""Static HealthProbe for readiness tests (implements ``paper_trail.ports.health.HealthProbe``)."""

from collections.abc import Mapping

from paper_trail.domain.health import ComponentHealth, HealthStatus


class StaticHealthProbe:
    """Reports a fixed status; counts how often it was checked."""

    def __init__(
        self,
        name: str,
        status: HealthStatus,
        detail: str | None = None,
        data: Mapping[str, str] | None = None,
    ) -> None:
        self._name = name
        self._status = status
        self._detail = detail
        self._data: Mapping[str, str] = dict(data or {})
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    def check(self) -> ComponentHealth:
        self.calls += 1
        return ComponentHealth(
            name=self._name, status=self._status, detail=self._detail, data=self._data
        )
