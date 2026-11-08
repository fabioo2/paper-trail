"""Tracing around the graph, so that nodes hold no tracing code (AC-A-09, plan 2.4).

``traced_node`` brackets a node with NODE start/end events; ``TracingLlm`` and
``TracingToolGateway`` decorate the ports so that every model call and every tool call, a
denial included, becomes one event. The runner applies all three.
"""

import hashlib
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from pydantic_core import to_jsonable_python

from paper_trail.agents.state import ReceiptState
from paper_trail.domain.enums import AgentRole, MatchSource
from paper_trail.domain.models import JsonValue
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.llm import ChatMessage, LlmError, LlmPort, LlmRequest, LlmResult
from paper_trail.ports.tools import ToolDescription, ToolGateway, ToolNotPermittedError
from paper_trail.ports.trace import Tracer


class Node(Protocol):
    """A graph node: the state in, a partial update out."""

    def __call__(self, state: ReceiptState) -> ReceiptState: ...


_CENTS = 100
# Prompts are named after their agent, except the extraction agent's vision prompt.
_PROMPT_AGENTS = {"vision": AgentRole.EXTRACTION}


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def traced_node(tracer: Tracer, agent: AgentRole, name: str, node: Node) -> Node:
    """``node`` with a NODE event when it starts and one when it ends. A node that raises or
    is interrupted has no end event: the run's ERROR or INTERRUPT event follows instead."""

    def run(state: ReceiptState) -> ReceiptState:
        tracer.emit(agent, TraceKind.NODE, name, "start", data={"phase": "start"})
        started = time.perf_counter()
        update = node(state)
        tracer.emit(
            agent,
            TraceKind.NODE,
            name,
            "done",
            data={"phase": "end"},
            duration_ms=_elapsed_ms(started),
        )
        return update

    return run


class TracingLlm:
    """An ``LlmPort`` that records each call on one receipt's trace: the prompt tag, model,
    attempt (1-based per tag), token counts, rendered messages and the validated output. An
    image is recorded as its SHA-256, never its bytes."""

    def __init__(self, inner: LlmPort, tracer: Tracer, receipt_id: int) -> None:
        self._inner = inner
        self._tracer = tracer
        self._receipt_id = receipt_id

    def complete_structured[M](self, request: LlmRequest, schema: type[M]) -> LlmResult[M]:
        prompt = request.tag.partition("/")[0]
        agent = _PROMPT_AGENTS.get(prompt) or AgentRole(prompt)
        data: dict[str, JsonValue] = {
            "tag": request.tag,
            "model": request.model,
            "attempt": self._attempt(request.tag),
            "messages": [_traced_message(message) for message in request.messages],
        }
        started = time.perf_counter()
        try:
            result = self._inner.complete_structured(request, schema)
        except LlmError as error:
            data |= {"prompt_tokens": None, "completion_tokens": None, "output": None}
            self._emit(
                agent,
                request.tag,
                f"{type(error).__name__}: {error}",
                data,
                started=started,
                error=error,
            )
            raise
        output: JsonValue = to_jsonable_python(result.value)
        data |= {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "output": output,
        }
        self._emit(agent, request.tag, _llm_summary(output), data, started=started, error=None)
        return result

    def _attempt(self, tag: str) -> int:
        earlier = self._tracer.events(self._receipt_id)
        return 1 + sum(1 for event in earlier if event.kind is TraceKind.LLM and event.name == tag)

    def _emit(  # noqa: PLR0913 - the parts of one LLM event
        self,
        agent: AgentRole,
        tag: str,
        summary: str,
        data: dict[str, JsonValue],
        *,
        started: float,
        error: LlmError | None,
    ) -> None:
        if error is not None:
            data["error"] = str(error)
        self._tracer.emit(
            agent, TraceKind.LLM, tag, summary, data=data, duration_ms=_elapsed_ms(started)
        )


class TracingToolGateway:
    """A ``ToolGateway`` that records each call as a TOOL event of the calling role, with
    ``allowed`` false for a denial (which is then raised, never retried)."""

    def __init__(self, inner: ToolGateway, tracer: Tracer) -> None:
        self._inner = inner
        self._tracer = tracer

    def call(
        self, role: AgentRole, name: str, payload: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        data: dict[str, JsonValue] = {"role": role.value, "tool": name, "allowed": True}
        started = time.perf_counter()
        try:
            result = self._inner.call(role, name, payload)
        except ToolNotPermittedError as error:
            data["allowed"] = False
            self._emit(role, name, str(error), data, started)
            raise
        except Exception as error:
            data["error"] = f"{type(error).__name__}: {error}"
            self._emit(role, name, f"ERROR {data['error']}", data, started)
            raise
        data["output"] = dict(result)
        self._emit(role, name, _tool_summary(name, result), data, started)
        return result

    def describe(self, role: AgentRole) -> tuple[ToolDescription, ...]:
        """Not a tool call, so not traced."""
        return self._inner.describe(role)

    def _emit(
        self, role: AgentRole, name: str, summary: str, data: dict[str, JsonValue], started: float
    ) -> None:
        self._tracer.emit(
            role, TraceKind.TOOL, name, summary, data=data, duration_ms=_elapsed_ms(started)
        )


def _traced_message(message: ChatMessage) -> JsonValue:
    traced: dict[str, JsonValue] = {"role": message.role, "content": message.content}
    if message.images:
        traced["images"] = [hashlib.sha256(image).hexdigest() for image in message.images]
    return traced


def _money(cents: object) -> str:
    return f"{cents / _CENTS:.2f}" if isinstance(cents, int) else "?"


def _llm_summary(output: JsonValue) -> str:
    """One line about an answer: an extraction's lines, total and confidence, a matching
    decision, or an enrichment's category and name, with the confidence."""
    if not isinstance(output, dict):
        return "ok"
    confidence = output.get("confidence")
    sure = f" · conf {confidence:.2f}" if isinstance(confidence, int | float) else ""
    if "decision" in output:
        product = output.get("product_id")
        return f"{output['decision']}{'' if product is None else f' #{product}'}{sure}"
    if "category" in output:
        return f"{output['category']} · {output.get('canonical_name')}{sure}"
    if "items" not in output:
        return "ok"
    items = output["items"]
    count = len(items) if isinstance(items, list) else 0
    return f"{count} lines · total {_money(output.get('total_cents'))}{sure}"


def _tool_summary(name: str, result: Mapping[str, JsonValue]) -> str:
    summarise = _TOOL_SUMMARIES.get(name)
    return "ok" if summarise is None else summarise(result)


def _ocr_summary(result: Mapping[str, JsonValue]) -> str:
    text, confidence, masked = result["text"], result["mean_confidence"], result["masked_pans"]
    parts = [f"{len(text) if isinstance(text, str) else 0:,} chars"]
    if isinstance(confidence, int | float):
        parts.append(f"ocr conf {confidence:.0%}")
    if rotation := result.get("rotation_degrees"):
        parts.append(f"rotated {rotation}°")
    if masked:
        parts.append(f"{masked} card{'s' if masked != 1 else ''} masked")
    return " · ".join(parts)


def _arithmetic_summary(result: Mapping[str, JsonValue]) -> str:
    issues = result["issues"]
    if result["ok"] or not isinstance(issues, list) or not issues:
        return "OK"
    first = issues[0]
    message = first.get("message") if isinstance(first, dict) else None
    more = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
    return f"FAIL · {message}{more}"


def _save_summary(result: Mapping[str, JsonValue]) -> str:
    return f"{str(result['status']).upper()} · {result['item_count']} items"


def _review_summary(result: Mapping[str, JsonValue]) -> str:
    return f"review #{result['review_id']}"


def _lookup_summary(result: Mapping[str, JsonValue]) -> str:
    product = result["product"]
    if not isinstance(product, dict):
        return "no match"
    return f"{result['source']} · #{product['id']} {product['canonical_name']}"


def _search_summary(result: Mapping[str, JsonValue]) -> str:
    candidates = result["candidates"]
    if not isinstance(candidates, list) or not candidates:
        return "no candidates"
    nearest = candidates[0]
    distance = nearest.get("distance") if isinstance(nearest, dict) else None
    near = f" · nearest {distance:.2f}" if isinstance(distance, float) else ""
    return f"{len(candidates)} candidate{'s' if len(candidates) != 1 else ''}{near}"


def _matches_summary(result: Mapping[str, JsonValue]) -> str:
    """The count per source first, in matching order (``memory 1 · alias 2``), then what was
    created: the terminal shows the start of it."""
    lines = result["lines"]
    sources = (
        [line.get("source") for line in lines if isinstance(line, dict)]
        if isinstance(lines, list)
        else []
    )
    counts = [f"{source} {sources.count(source)}" for source in MatchSource if source in sources]
    created, prices = result["new_products"], result["prices"]
    tail = [f"+{created} product{'s' if created != 1 else ''}"] if created else []
    tail.append(f"{prices} price{'s' if prices != 1 else ''}")
    return " · ".join(counts + tail) if counts else "no lines to match"


def _enrichment_summary(result: Mapping[str, JsonValue]) -> str:
    """The stored category first (``memory`` when a person's correction chose it)."""
    product = result["product"]
    if not isinstance(product, dict):
        return "ok"
    memory = " (memory)" if result["category_source"] == "memory" else ""
    return f"{product['category']}{memory} · #{product['id']} {product['canonical_name']}"


_TOOL_SUMMARIES: dict[str, Callable[[Mapping[str, JsonValue]], str]] = {
    "ocr_read": _ocr_summary,
    "check_arithmetic": _arithmetic_summary,
    "save_receipt": _save_summary,
    "open_review": _review_summary,
    "product_lookup": _lookup_summary,
    "product_similarity_search": _search_summary,
    "save_matches": _matches_summary,
    "save_enrichment": _enrichment_summary,
}
