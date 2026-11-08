"""How a tool's error crosses the MCP wire and becomes the same exception on the other side.

The MCP server answers a failed call with an error result whose ``structuredContent`` is
``{"error": <kind>, "message": <str(error)>}``; ``McpToolGateway`` raises the class of that kind
again, so a node, the tracer and the CLI see the same error over both transports (S5-AC-04).
A denial is rebuilt from the call's own role and tool. An error of no listed class becomes a
``ToolFailedError`` whose message starts with the original class name.
"""

from collections.abc import Mapping

from paper_trail.domain.enums import AgentRole
from paper_trail.domain.errors import (
    ConflictError,
    DomainError,
    DomainValidationError,
    NotFoundError,
    UnsupportedMediaError,
)
from paper_trail.domain.models import JsonValue
from paper_trail.ports.llm import LlmOutputError, LlmUnavailableError
from paper_trail.ports.ocr import OcrError
from paper_trail.ports.tools import (
    ToolFailedError,
    ToolInputError,
    ToolNotPermittedError,
    ToolOutputError,
    UnknownToolError,
)

DENIED = "denied"
FAILED = "failed"

# Most specific first: an error is sent as the first kind it is an instance of.
_KINDS: tuple[tuple[str, type[Exception]], ...] = (
    (DENIED, ToolNotPermittedError),
    ("unknown_tool", UnknownToolError),
    ("invalid_input", ToolInputError),
    ("invalid_output", ToolOutputError),
    ("not_found", NotFoundError),
    ("conflict", ConflictError),
    ("unsupported_media", UnsupportedMediaError),
    ("invalid", DomainValidationError),
    ("domain", DomainError),
    ("ocr", OcrError),
    # The embedding host: unavailable stops the run with its checkpoint (S3-AC-10) on both
    # transports, so it must not arrive as a ToolFailedError.
    ("llm_unavailable", LlmUnavailableError),
    ("llm_output", LlmOutputError),
)
_CLASSES = dict(_KINDS)


def error_payload(error: Exception) -> dict[str, JsonValue]:
    """The structured content of the error result for ``error``."""
    kind = next((kind for kind, cls in _KINDS if isinstance(error, cls)), FAILED)
    message = str(error) if kind != FAILED else f"{type(error).__name__}: {error}"
    return {"error": kind, "message": message}


def error_from_payload(
    payload: Mapping[str, JsonValue] | None, role: AgentRole, tool: str, fallback: str
) -> Exception:
    """The exception an error result stands for; ``fallback`` is its text content, used when
    the result carries no structured error (it did not come from this project's server)."""
    kind = payload.get("error") if payload else None
    message = payload.get("message") if payload else None
    text = message if isinstance(message, str) else fallback
    if kind == DENIED:
        return ToolNotPermittedError(role, tool)
    cls = _CLASSES.get(kind) if isinstance(kind, str) else None
    return ToolFailedError(text) if cls is None else cls(text)
