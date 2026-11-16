"""Tracing around the graph, so that nodes hold no tracing code (AC-A-09, plan 2.4).

``traced_node`` brackets a node with NODE start/end events; ``TracingReader`` and
``TracingToolGateway`` decorate the ports so that every Claude read and every tool call, a
denial included, becomes one event. A save that leaves a discount at receipt level or an item
line without a price also gets a NOTE naming them, so neither goes unexplained. The runner
applies all three.
"""

import hashlib
import time
from collections.abc import Callable, Mapping
from typing import Protocol

from pydantic_core import to_jsonable_python

from paper_trail.agents.json_values import masked_json
from paper_trail.agents.state import ReceiptState
from paper_trail.domain.enums import AgentRole, MatchSource
from paper_trail.domain.models import JsonValue
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.reader import ReaderError, ReadRequest, ReadResult, ReceiptReader
from paper_trail.ports.tools import ToolDescription, ToolGateway, ToolNotPermittedError
from paper_trail.ports.trace import Tracer


class Node(Protocol):
    """A graph node: the state in, a partial update out."""

    def __call__(self, state: ReceiptState) -> ReceiptState: ...


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


class TracingReader:
    """A ``ReceiptReader`` that records each read on one receipt's trace as an LLM event named
    after the request's tag: model, the request's attempt (1-based, right across resumed runs),
    input and output tokens, stop reason, and the SHA-256 of the image sent, never its bytes. A
    failed read records the error's class and message (card numbers masked), and tokens only
    when it was billed."""

    def __init__(self, inner: ReceiptReader, tracer: Tracer, receipt_id: int) -> None:
        self._inner = inner
        self._tracer = tracer
        self._receipt_id = receipt_id

    def read[M](self, request: ReadRequest, schema: type[M]) -> ReadResult[M]:
        data: dict[str, JsonValue] = {
            "tag": request.tag,
            "model": request.model,
            "attempt": request.attempt,
        }
        started = time.perf_counter()
        try:
            result = self._inner.read(request, schema)
        except ReaderError as error:
            usage = error.usage
            failure = f"{type(error).__name__}: {mask_pans(str(error)).text}"
            data |= {
                "image_sha256": _sent_sha256(request, error.image_sha256),
                "input_tokens": None if usage is None else usage.input_tokens,
                "output_tokens": None if usage is None else usage.output_tokens,
                "stop_reason": None,
                "output": None,
                "error": failure,
            }
            self._emit(request.tag, failure, data, started)
            raise
        output, _ = masked_json(to_jsonable_python(result.value))
        data |= {
            "image_sha256": _sent_sha256(request, result.image_sha256),
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "stop_reason": result.stop_reason,
            "output": output,
        }
        self._emit(request.tag, f"{request.model} · {_read_summary(output)}", data, started)
        return result

    def _emit(self, tag: str, summary: str, data: dict[str, JsonValue], started: float) -> None:
        self._tracer.emit(
            AgentRole.EXTRACTION,
            TraceKind.LLM,
            tag,
            summary,
            data=data,
            duration_ms=_elapsed_ms(started),
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
        note = _TOOL_NOTES.get(name)
        found = None if note is None else note(result)
        if found is not None:
            note_name, summary, note_data = found
            self._tracer.emit(role, TraceKind.NOTE, note_name, summary, data=note_data)
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


def _sent_sha256(request: ReadRequest, reported: str | None) -> str | None:
    """The SHA-256 of the image the reader sent: as it reports it (it may have re-encoded the
    image to fit the API), else of the request's image, sent as it is (None: no image)."""
    if reported or request.image is None:
        return reported
    return hashlib.sha256(request.image).hexdigest()


def _read_summary(output: JsonValue) -> str:
    """One line about an answer: its items and its total as printed."""
    if not isinstance(output, dict):
        return "ok"
    items = output.get("items")
    count = len(items) if isinstance(items, list) else 0
    return f"{count} item{'s' if count != 1 else ''} · total {output.get('total')}"


def _tool_summary(name: str, result: Mapping[str, JsonValue]) -> str:
    summarise = _TOOL_SUMMARIES.get(name)
    return "ok" if summarise is None else summarise(result)


def _arithmetic_summary(result: Mapping[str, JsonValue]) -> str:
    issues = result["issues"]
    if result["ok"] or not isinstance(issues, list) or not issues:
        return "OK"
    first = issues[0]
    message = first.get("message") if isinstance(first, dict) else None
    more = f" (+{len(issues) - 1} more)" if len(issues) > 1 else ""
    return f"FAIL · {message}{more}"


def _save_summary(result: Mapping[str, JsonValue]) -> str:
    """Status and items, then the discounts when the receipt has any discount line."""
    summary = f"{str(result['status']).upper()} · {result['item_count']} items"
    linked = result.get("discounts_linked")
    receipt_level = result.get("discounts_receipt_level")
    level = len(receipt_level) if isinstance(receipt_level, list) else 0
    if not linked and not level:
        return summary
    return f"{summary} · discounts {linked or 0} linked · {level} receipt-level"


def _review_summary(result: Mapping[str, JsonValue]) -> str:
    return f"review #{result['review_id']}"


def _lookup_summary(result: Mapping[str, JsonValue]) -> str:
    product = result["product"]
    if not isinstance(product, dict):
        return "no match"
    return f"{result['source']} · #{product['id']} {product['canonical_name']}"


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


def _catalog_lookup_summary(result: Mapping[str, JsonValue]) -> str:
    """The code as read, then ours or the source that knows it."""
    code = result["code"]
    if not isinstance(code, dict):
        return "no code"
    if code.get("scheme") is None:
        return "not a product code"
    read = f"{code['scheme']} {code['identity']}"
    product, entry = result["product"], result["entry"]
    if isinstance(product, dict):
        return f"{read} · ours #{product['id']} {product['canonical_name']}"
    if isinstance(entry, dict):
        return f"{read} · {entry['source']} {entry['name']}"
    return f"{read} · no source"


def _describe_summary(result: Mapping[str, JsonValue]) -> str:
    """The stored category first, then the product."""
    product = result["product"]
    if not isinstance(product, dict):
        return "ok"
    return f"{product['category'] or '-'} · #{product['id']} {product['canonical_name']}"


_CATALOG_SCHEMES = ("store", "gtin", "plu")
_NAME_SOURCES = {"receipt_text": "printed text", "plu": "plu"}


def match_counts_summary(counts: Mapping[str, JsonValue]) -> str:
    """The matching NOTE's text: each source in ``MatchSource`` order with its count, CATALOG
    split by code scheme (store, gtin, plu) and NEW by name source (``receipt_text`` shown as
    "printed text", then plu), zero parts left out, e.g. ``catalog 9 (store 5, gtin 3, plu 1) ·
    alias 1 · new 4 (printed text 3, plu 1)``. ``counts`` maps a source to a count, or for
    ``catalog`` and ``new`` to counts by scheme or name source."""
    parts: list[str] = []
    for source in MatchSource:
        count = counts.get(source.value)
        if isinstance(count, dict):
            labels = (
                {scheme: scheme for scheme in _CATALOG_SCHEMES}
                if source is MatchSource.CATALOG
                else _NAME_SOURCES
            )
            shown = [
                (label, number)
                for key, label in labels.items()
                if isinstance(number := count.get(key), int) and number
            ]
            total = sum(number for _, number in shown)
            if total:
                inner = ", ".join(f"{label} {number}" for label, number in shown)
                parts.append(f"{source.value} {total} ({inner})")
        elif isinstance(count, int) and count:
            parts.append(f"{source.value} {count}")
    return " · ".join(parts)


type _Note = tuple[str, str, dict[str, JsonValue]]


def _entries(value: JsonValue) -> list[dict[str, JsonValue]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _discounts_note(result: Mapping[str, JsonValue]) -> _Note | None:
    """The discounts a saved receipt keeps at receipt level and why (0-based positions)."""
    receipt_level = _entries(result.get("discounts_receipt_level"))
    if not receipt_level:
        return None
    named = "; ".join(
        f"pos {entry['position']} `{entry['raw_text']}` ({entry['reason']})"
        for entry in receipt_level
    )
    return "discounts", f"kept at receipt level: {named}", {"receipt_level": [*receipt_level]}


def _unpriced_note(result: Mapping[str, JsonValue]) -> _Note | None:
    """The matched item lines left without a price because their total is negative."""
    unpriced = _entries(result.get("unpriced"))
    if not unpriced:
        return None
    named = "; ".join(f"pos {entry['position']} `{entry['raw_text']}`" for entry in unpriced)
    return "prices", f"no price for a negative item line: {named}", {"unpriced": [*unpriced]}


_TOOL_NOTES: dict[str, Callable[[Mapping[str, JsonValue]], _Note | None]] = {
    "save_receipt": _discounts_note,
    "save_matches": _unpriced_note,
}

_TOOL_SUMMARIES: dict[str, Callable[[Mapping[str, JsonValue]], str]] = {
    "check_arithmetic": _arithmetic_summary,
    "save_receipt": _save_summary,
    "open_review": _review_summary,
    "product_lookup": _lookup_summary,
    "save_matches": _matches_summary,
    "catalog_lookup": _catalog_lookup_summary,
    "describe_from_catalog": _describe_summary,
}
