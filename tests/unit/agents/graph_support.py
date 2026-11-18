"""Helpers shared by the graph suites: the scripted reader of a harness, a second process on
the same data, a run's outcome, and a tracer that keeps what it is given."""

import copy
import itertools
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from paper_trail.agents.runner import LangGraphReceiptRunner
from paper_trail.agents.state import thread_id
from paper_trail.application.process_receipt import ProcessOutcome, ProcessReceiptFile
from paper_trail.application.receipts import ResolveReview
from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.domain.trace import TraceEvent, TraceKind
from paper_trail.ports.agents import GraphOutcome
from tests.agent_harness import Harness, build_harness, receipt_hold
from tests.fakes.reader import Reply, ScriptedReader
from tests.fakes.trace import FakeTracer
from tests.support import require_id

_LABELS = itertools.count(1)


def scripted(harness: Harness) -> ScriptedReader:
    reader = harness.reader
    assert isinstance(reader, ScriptedReader), reader
    return reader


def second(first: Harness, *replies: Reply) -> Harness:
    """Another process on the same database, checkpoints and images; with no reply scripted,
    any read fails the test."""
    return build_harness(
        ScriptedReader(*replies),
        settings=first.settings,
        clock=first.clock,
        checkpointer=first.checkpointer,
        uow_factory=first.uow_factory,
        images=first.images,
    )


def graph(outcome: ProcessOutcome) -> GraphOutcome:
    assert outcome.failure is None, outcome.failure
    assert outcome.graph is not None
    return outcome.graph


def fresh_label() -> str:
    """A photo label no other run in this session used: a new image, never a duplicate."""
    return f"photo-{next(_LABELS)}"


def config(receipt_id: int) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id(receipt_id), "checkpoint_ns": ""}}


def saved_values(saver: BaseCheckpointSaver[Any], receipt_id: int) -> dict[str, Any]:
    saved = saver.get_tuple(config(receipt_id))
    assert saved is not None, f"receipt {receipt_id} has no checkpoint"
    values: dict[str, Any] = saved.checkpoint["channel_values"]
    return values


def review_messages(harness: Harness, review_id: int | None) -> list[JsonValue]:
    issues = harness.review(require_id(review_id)).details["issues"]
    assert isinstance(issues, list)
    return [issue["message"] for issue in issues if isinstance(issue, dict)]


class RawTracer(FakeTracer):
    """``FakeTracer`` storing every event exactly as emitted: it masks nothing, so a card number
    reaching it is a leak of the code that emitted it."""

    def emit(  # noqa: PLR0913 - one argument per caller-supplied TraceEvent field
        self,
        agent: AgentRole,
        kind: TraceKind,
        name: str,
        summary: str,
        *,
        data: Mapping[str, JsonValue] | None = None,
        duration_ms: int | None = None,
    ) -> TraceEvent:
        receipt_id = self._bound[-1]
        events = self._events[receipt_id]
        event = TraceEvent(
            seq=len(events) + 1,
            at=self._clock.now(),
            receipt_id=receipt_id,
            agent=agent,
            kind=kind,
            name=name,
            summary=summary,
            data=dict(data or {}),
            duration_ms=duration_ms,
        )
        events.append(event)
        return event


def raw_traced(harness: Harness) -> Harness:
    """``harness`` with a ``RawTracer`` behind its runner, processing and review answers."""
    tracer = RawTracer(harness.clock)
    deps = replace(harness.deps, tracer=tracer)
    runner = LangGraphReceiptRunner(deps, harness.checkpointer)
    return replace(
        harness,
        tracer=tracer,
        deps=deps,
        runner=runner,
        process=ProcessReceiptFile(
            harness.images,
            harness.uow_factory,
            runner,
            tracer,
            harness.clock,
            hold=receipt_hold(harness.uow_factory),
        ),
        resolve=ResolveReview(
            harness.uow_factory,
            runner,
            harness.record,
            harness.clock,
            hold=receipt_hold(harness.uow_factory),
        ),
    )


def rewrite_latest(
    saver: BaseCheckpointSaver[Any], receipt_id: int, change: Callable[[dict[str, Any]], None]
) -> None:
    """Replace the thread's latest checkpoint (same id) by one whose values ``change`` edited:
    what an older version would have saved."""
    saved = saver.get_tuple(config(receipt_id))
    assert saved is not None
    checkpoint = copy.copy(saved.checkpoint)
    values = dict(checkpoint["channel_values"])
    change(values)
    versions = {k: v for k, v in checkpoint["channel_versions"].items() if k in values}
    checkpoint["channel_values"] = values
    checkpoint["channel_versions"] = versions
    saver.put(saved.parent_config or config(receipt_id), checkpoint, saved.metadata, versions)
