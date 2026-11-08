"""The in-process ``ToolGateway``: calls the registry directly, JSON in and out (ADR-0013)."""

from collections.abc import Mapping

from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.ports.tools import ToolDescription
from paper_trail.tools.registry import ToolRegistry


class InProcessToolGateway:
    """Implements ``paper_trail.ports.tools.ToolGateway``."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def call(
        self, role: AgentRole, name: str, payload: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        result: dict[str, JsonValue] = (
            self._registry.for_role(role).call(name, payload).model_dump(mode="json")
        )
        return result

    def describe(self, role: AgentRole) -> tuple[ToolDescription, ...]:
        return self._registry.for_role(role).describe()
