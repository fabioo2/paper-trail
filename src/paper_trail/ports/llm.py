"""The language model (PLAN 5, ADR-0009): a chat request in, a validated structured answer out."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

type ChatRole = Literal["system", "user", "assistant"]


class LlmError(Exception):
    """Base class of the model's failures."""


class LlmUnavailableError(LlmError):
    """The LLM host cannot be reached, timed out or failed; the message names the host.

    Not a failure of the receipt: the run stops with its checkpoint and can resume later.
    """


class LlmOutputError(LlmError):
    """The model's answer did not match the schema, even after one repair attempt."""


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: ChatRole
    content: str
    images: Sequence[bytes] = ()  # for a vision model


@dataclass(frozen=True, slots=True)
class LlmRequest:
    model: str
    tag: str  # the prompt id, e.g. "extraction/v1"; recorded in the trace
    messages: Sequence[ChatMessage]
    temperature: float = 0.0
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LlmResult[M]:
    value: M
    prompt_tokens: int | None  # None when the host did not report them
    completion_tokens: int | None


class LlmPort(Protocol):
    def complete_structured[M](self, request: LlmRequest, schema: type[M]) -> LlmResult[M]:
        """The answer validated as ``schema`` (a Pydantic model in practice).

        Raises LlmOutputError when it does not validate, LlmUnavailableError when the host
        cannot be reached.
        """
        ...
