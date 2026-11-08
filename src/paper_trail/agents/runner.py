"""``ReceiptGraphRunner`` on LangGraph: one checkpointed thread per receipt (ADR-0008).

Each run compiles the graph over ports decorated for that receipt's trace, binds the tracer to
the receipt, and invokes with durable (synchronous) checkpoints, so a run that stops because
the LLM host went away, or at a static breakpoint (``interrupt_after``), resumes from its last
completed node. That node is read from the checkpoint history: the latest checkpoint's parent
was written just before it ran.
"""

from dataclasses import replace
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command, StateSnapshot

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.drafts import extraction_draft
from paper_trail.agents.graph import ReceiptGraph, build_receipt_graph
from paper_trail.agents.state import ReceiptState, thread_id
from paper_trail.agents.tracing import TracingLlm, TracingToolGateway
from paper_trail.application.payloads import DraftPayload
from paper_trail.domain.enums import AgentRole, ReceiptStatus, ReviewReason
from paper_trail.domain.errors import ConflictError
from paper_trail.domain.models import ReceiptDraft
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.agents import GraphOutcome, NothingToResumeError

REVIEW_NODE = "review"
_START = "__start__"


class LangGraphReceiptRunner:
    """Implements ``paper_trail.ports.agents.ReceiptGraphRunner``."""

    def __init__(self, deps: AgentDeps, checkpointer: BaseCheckpointSaver[Any]) -> None:
        self._deps = deps
        self._checkpointer = checkpointer

    def start(
        self,
        receipt_id: int,
        *,
        source_name: str,
        image_path: str | None = None,
        ocr_text: str | None = None,
        interrupt_after: str | None = None,
    ) -> GraphOutcome:
        initial: ReceiptState = {
            "receipt_id": receipt_id,
            "source_name": source_name,
            "image_path": image_path,
            "ocr_text": ocr_text,
            "text_attempts": 0,
            "vision_attempts": 0,
            "approved": False,
        }
        return self._run(receipt_id, initial, interrupt_after=interrupt_after)

    def resume(self, receipt_id: int, draft: ReceiptDraft | None = None) -> GraphOutcome:
        """Continue the receipt's thread: answer the review it waits for (``draft``, or None to
        accept the extraction it holds), or, when it is not waiting (it paused at a breakpoint
        or its last run raised), run again from the last checkpoint, the failed node from its
        start."""
        graph = build_receipt_graph(self._deps, self._checkpointer)
        snapshot = self._snapshot(graph, receipt_id)
        if _paused_for_review(snapshot):
            answer = (
                None if draft is None else DraftPayload.from_draft(draft).model_dump(mode="json")
            )
            return self._run(receipt_id, Command(resume=answer), resumed_from=REVIEW_NODE)
        if draft is not None:
            msg = f"receipt {receipt_id} is not waiting for a review"
            raise ConflictError(msg)
        if not snapshot.next:
            state = "has finished" if snapshot.values else "does not exist"
            msg = f"nothing to resume: the agents' run of receipt {receipt_id} {state}"
            raise NothingToResumeError(msg)
        # LangGraph continues a paused or failed thread from None, not Command(resume=...).
        return self._run(receipt_id, None, resumed_from=_last_completed(graph, snapshot))

    def reset(self, receipt_id: int) -> None:
        self._checkpointer.delete_thread(thread_id(receipt_id))

    def pending_draft(self, receipt_id: int) -> ReceiptDraft | None:
        snapshot = self._snapshot(build_receipt_graph(self._deps, self._checkpointer), receipt_id)
        state = _state(snapshot)
        if not _paused_for_review(snapshot) or state.get("extraction") is None:
            return None
        return extraction_draft(self._deps, state)

    def _run(
        self,
        receipt_id: int,
        graph_input: ReceiptState | Command[Any] | None,
        *,
        interrupt_after: str | None = None,
        resumed_from: str | None = None,
    ) -> GraphOutcome:
        tracer = self._deps.tracer
        traced = replace(
            self._deps,
            llm=TracingLlm(self._deps.llm, tracer, receipt_id),
            tools=TracingToolGateway(self._deps.tools, tracer),
        )
        with tracer.bind(receipt_id):
            graph = build_receipt_graph(traced, self._checkpointer)
            graph.invoke(
                graph_input,
                _config(receipt_id),
                interrupt_after=None if interrupt_after is None else [interrupt_after],
                durability="sync",
            )
            snapshot = self._snapshot(graph, receipt_id)
            paused_at = (
                _last_completed(graph, snapshot)
                if snapshot.next and not _paused_for_review(snapshot)
                else None
            )
            outcome = _outcome(receipt_id, snapshot, paused_at, resumed_from)
            self._emit_interrupt(outcome)
        return outcome

    def _emit_interrupt(self, outcome: GraphOutcome) -> None:
        """The run's last event when it stops for a person or at a breakpoint."""
        if outcome.paused_at is not None:
            self._deps.tracer.emit(
                AgentRole.SUPERVISOR,
                TraceKind.INTERRUPT,
                outcome.paused_at,
                f"paused after {outcome.paused_at}",
                data={"paused_at": outcome.paused_at},
            )
        elif outcome.review_id is not None and outcome.review_reason is not None:
            reason = outcome.review_reason.value
            self._deps.tracer.emit(
                AgentRole.SUPERVISOR,
                TraceKind.INTERRUPT,
                REVIEW_NODE,
                f"NEEDS_REVIEW #{outcome.review_id} {reason}",
                data={"review_id": outcome.review_id, "reason": reason},
            )

    @staticmethod
    def _snapshot(graph: ReceiptGraph, receipt_id: int) -> StateSnapshot:
        return graph.get_state(_config(receipt_id))


def _config(receipt_id: int) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id(receipt_id)}}


def _state(snapshot: StateSnapshot) -> ReceiptState:
    # LangGraph types checkpointed values as a plain dict; they are this graph's state.
    return cast("ReceiptState", snapshot.values)


def _paused_for_review(snapshot: StateSnapshot) -> bool:
    return REVIEW_NODE in snapshot.next


def _last_completed(graph: ReceiptGraph, snapshot: StateSnapshot) -> str | None:
    """The node that wrote the thread's latest checkpoint: the one its parent was about to
    run. None when nothing has run yet."""
    if snapshot.parent_config is None:
        return None
    ran = graph.get_state(snapshot.parent_config).next
    return ran[0] if ran and ran[0] != _START else None


def _outcome(
    receipt_id: int, snapshot: StateSnapshot, paused_at: str | None, resumed_from: str | None
) -> GraphOutcome:
    state = _state(snapshot)
    in_review = _paused_for_review(snapshot)
    reason = state.get("review_reason")
    saved = state.get("status")
    if in_review:
        status = ReceiptStatus.NEEDS_REVIEW
    elif saved is not None:
        status = ReceiptStatus(saved)
    elif paused_at is not None:
        status = ReceiptStatus.PROCESSING  # paused before persist: nothing is saved yet
    else:
        msg = f"the graph of receipt {receipt_id} ended without saving it"
        raise RuntimeError(msg)
    return GraphOutcome(
        receipt_id=receipt_id,
        status=status,
        review_id=state.get("review_id") if in_review else None,
        review_reason=ReviewReason(reason) if in_review and reason is not None else None,
        text_attempts=state.get("text_attempts", 0),
        vision_attempts=state.get("vision_attempts", 0),
        paused_at=paused_at,
        resumed_from=resumed_from,
    )
