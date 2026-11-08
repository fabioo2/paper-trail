"""Tool gateway decorators for tests (implement ``paper_trail.ports.tools.ToolGateway``).

Each wraps a real gateway (in-process or MCP) and changes one thing about one tool, so a test
can show how the graph behaves when an allowlist is misconfigured, a node calls a tool in the
wrong role, or the process dies between a tool's write and the next checkpoint; or it records
what the nodes sent.
"""

from collections.abc import Mapping

from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.ports.tools import ToolDescription, ToolGateway, ToolNotPermittedError


class _Decorator:
    def __init__(self, inner: ToolGateway) -> None:
        self._inner = inner

    def describe(self, role: AgentRole) -> tuple[ToolDescription, ...]:
        return self._inner.describe(role)


class DenyingToolGateway(_Decorator):
    """Denies one tool as if it were outside every role's allowlist; passes the rest through.

    Stands in for a misconfigured allowlist so a test can show that a denial fails the run
    loudly instead of being swallowed or retried (S5-AC-05, AC-B-02).
    """

    def __init__(self, inner: ToolGateway, denied_tool: str) -> None:
        super().__init__(inner)
        self._denied_tool = denied_tool

    def call(
        self, role: AgentRole, name: str, payload: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        if name == self._denied_tool:
            raise ToolNotPermittedError(role, name)
        return self._inner.call(role, name, payload)


class MisroutedToolGateway(_Decorator):
    """Sends every call of one tool in another role, as a node with a wrong role would.

    Unlike ``DenyingToolGateway`` the denial comes from the real registry (and, over MCP, from
    the real server), so its ``tool.denied`` log line is the production one (S5-AC-05, AC-B-02).
    """

    def __init__(self, inner: ToolGateway, tool: str, as_role: AgentRole) -> None:
        super().__init__(inner)
        self._tool = tool
        self._as_role = as_role

    def call(
        self, role: AgentRole, name: str, payload: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        return self._inner.call(self._as_role if name == self._tool else role, name, payload)


class SimulatedCrashError(RuntimeError):
    """The process died: raised after a tool's write has committed, before the node returns."""


class CrashAfterToolGateway(_Decorator):
    """Lets the first call of one tool complete, then raises ``SimulatedCrashError``.

    The tool's write is committed but the node never returns, so LangGraph writes no checkpoint
    for it: on resume the node runs again from its start (slice A review note 4).
    """

    def __init__(self, inner: ToolGateway, tool: str) -> None:
        super().__init__(inner)
        self._tool = tool
        self.crashed = False

    def call(
        self, role: AgentRole, name: str, payload: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        result = self._inner.call(role, name, payload)
        if name == self._tool and not self.crashed:
            self.crashed = True
            msg = f"simulated crash after {name}"
            raise SimulatedCrashError(msg)
        return result


class RecordingToolGateway(_Decorator):
    """Passes every call through and records ``(role, name, payload)``: what a node sent, which
    the trace does not keep (its TOOL events hold the output only)."""

    def __init__(self, inner: ToolGateway) -> None:
        super().__init__(inner)
        self.calls: list[tuple[AgentRole, str, Mapping[str, JsonValue]]] = []

    def call(
        self, role: AgentRole, name: str, payload: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        self.calls.append((role, name, payload))
        return self._inner.call(role, name, payload)

    def payloads(self, name: str) -> list[Mapping[str, JsonValue]]:
        return [payload for _, called, payload in self.calls if called == name]
