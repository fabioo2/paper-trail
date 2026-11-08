"""The agent trace on the terminal: one block per receipt, at most 100 columns (plan 4.2).

    ━━ receipt 12 · costco.jpg · run e1 · extraction/v1 · inprocess ━━
      #  agent       step                        result                                   time
      1  extraction  tool ocr_read               1,284 chars · ocr conf 81% · 1 card ma…  0.9s
      ...
      ✔ COMPLETED in 24.0s · llm 2 · tools 4 (0 denied) · trace /data/runs/e1/traces/...

NODE events are left out (they bracket the rows). With ``verbose`` the feedback sent back to
the model and the model's answers (masked) are printed under their rows.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import typer

from paper_trail.domain.trace import TraceEvent, TraceKind

RESULT_WIDTH = 42
STEP_WIDTH = 27
_MS_PER_S = 1000
_KIND_COLORS = {
    TraceKind.ERROR: "red",
    TraceKind.INTERRUPT: "yellow",
    TraceKind.ROUTE: "cyan",
}


class Style(Protocol):
    """Colours (a Typer colour name) and/or emboldens a text; plain text with colour off."""

    def __call__(self, text: str, fg: str | None = None, *, bold: bool = False) -> str: ...


def styler(*, color: bool) -> Style:
    def style(text: str, fg: str | None = None, *, bold: bool = False) -> str:
        return typer.style(text, fg=fg, bold=bold) if color else text

    return style


@dataclass(frozen=True, slots=True)
class ReceiptHeader:
    receipt_id: int
    source_name: str
    run: str
    prompt_tag: str
    transport: str


def header(info: ReceiptHeader) -> str:
    parts = [f"receipt {info.receipt_id}", info.source_name, f"run {info.run}"]
    return f"━━ {' · '.join([*parts, info.prompt_tag, info.transport])} ━━"


def rows(events: Sequence[TraceEvent], style: Style, *, verbose: bool) -> list[str]:
    shown = [event for event in events if event.kind is not TraceKind.NODE]
    lines = [f"  {'#':>2}  {'agent':<10}  {'step':<{STEP_WIDTH}} {'result':<{RESULT_WIDTH}} time"]
    for number, event in enumerate(shown, start=1):
        denied = event.kind is TraceKind.TOOL and event.data.get("allowed") is False
        text = f"✖ {event.summary}" if denied else event.summary
        cell = _cut(text, RESULT_WIDTH).ljust(RESULT_WIDTH)
        color = "red" if denied else _KIND_COLORS.get(event.kind)
        if color is not None:
            cell = style(cell, color)
        took = "" if event.duration_ms is None else f" {event.duration_ms / _MS_PER_S:>4.1f}s"
        step = _step(event)
        lines.append(f"  {number:>2}  {event.agent.value:<10}  {step:<{STEP_WIDTH}} {cell}{took}")
        if verbose:
            lines += _details(event)
    return [line.rstrip() for line in lines]


def _step(event: TraceEvent) -> str:
    if event.kind is TraceKind.ROUTE:
        return f"route -> {event.name}"
    return f"{event.kind.value} {event.name}"


def _details(event: TraceEvent) -> list[str]:
    """The text behind a row: feedback for a retry, the answer of a model call."""
    if event.kind is TraceKind.ROUTE:
        feedback = event.data.get("feedback")
        lines = feedback if isinstance(feedback, list) else []
        return [f"        feedback: {line}" for line in lines]
    if event.kind is TraceKind.LLM:
        output = event.data.get("output")
        if output is None:
            return [f"        error: {event.data.get('error')}"]
        return [f"        answer: {json.dumps(output, ensure_ascii=False)}"]
    return []


def _cut(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"
