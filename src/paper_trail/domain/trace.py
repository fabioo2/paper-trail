"""What happened while the agents worked on a receipt: one event per step (agent-core plan 2.2).

Events are the per-agent trace shown on the terminal and written as JSON; they hold masked
data only (ADR-0011).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, auto

from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue


class TraceKind(StrEnum):
    NODE = auto()  # a graph node started or ended
    TOOL = auto()  # a tool call, allowed or denied
    LLM = auto()  # a model call
    ROUTE = auto()  # the supervisor's decision
    INTERRUPT = auto()  # the run paused for a person
    ERROR = auto()  # the run failed
    NOTE = auto()  # a node changed what a model answered, visibly (e.g. a reconciled multi-buy)


@dataclass(frozen=True, slots=True)
class TraceEvent:
    seq: int  # 1, 2, ... per receipt, across resumed runs
    at: datetime
    receipt_id: int
    agent: AgentRole
    kind: TraceKind
    name: str  # node name, tool name, prompt tag ("extraction/v1") or route target
    summary: str  # one terminal line, at most 90 characters
    data: Mapping[str, JsonValue]  # the full detail, for the JSON trace
    duration_ms: int | None
