"""``supervisor``: routes deterministically (``route``) and owns the writes (ADR-0008).

A retry gets feedback: the validation issues verbatim, or with feedback off (experiment E1's
control arm) one generic line. The first vision attempt gets none (ADR-0017): the text
attempts' numbers must not anchor the vision model; a vision retry gets the issues of the
previous vision answer. A receipt without a photo (a text input) is never sent to vision
(AC-D-03), and a photo with a poor OCR read goes to vision first (``first_attempt_after_read``,
the supervisor's policy on the ``read`` edge). Sending a receipt to review opens the review item
here, through the supervisor's ``open_review`` tool, so that the ``review`` node only waits for
a person.
"""

from collections.abc import Callable

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.json_values import as_int
from paper_trail.agents.routing import (
    ALWAYS_VISION_FIRST,
    GraphLimits,
    RouteDecision,
    RoutingFacts,
    Target,
    effective_limits,
    first_attempt,
    route,
)
from paper_trail.agents.state import ReceiptState, thread_id
from paper_trail.agents.tracing import Node
from paper_trail.config import Settings
from paper_trail.domain.enums import AgentRole, ReviewReason
from paper_trail.domain.models import JsonValue
from paper_trail.domain.trace import TraceKind

GENERIC_RETRY = "The previous answer was inconsistent. Read the receipt again."
UNPARSED_RETRY = "The previous answer did not match the JSON schema: {error}"


RETRY_TARGETS = frozenset({"extract_text", "extract_vision"})


def graph_limits(settings: Settings) -> GraphLimits:
    """The limits the graph routes with (``GRAPH__*``, after the ``process`` flags)."""
    graph = settings.graph
    return GraphLimits(
        max_text_attempts=graph.max_text_attempts,
        max_vision_attempts=graph.max_vision_attempts,
        min_confidence=graph.min_confidence,
        feedback=graph.feedback,
        vision_first_below=graph.vision_first_below,
    )


def _limits(configured: GraphLimits, state: ReceiptState) -> GraphLimits:
    return effective_limits(
        configured,
        has_photo=state.get("image_path") is not None,
        ocr_confidence=state.get("ocr_confidence"),
    )


def first_attempt_after_read(deps: AgentDeps) -> Callable[[ReceiptState], Target]:
    """The ``read`` edge: the text model first, or the vision model when the OCR read is poor,
    which is traced as a ROUTE event before any model call."""
    configured = graph_limits(deps.settings)

    def after_read(state: ReceiptState) -> Target:
        target = first_attempt(_limits(configured, state))
        if target == "extract_vision":
            confidence = state.get("ocr_confidence") or 0.0
            below = configured.vision_first_below
            why = "every photo" if below >= ALWAYS_VISION_FIRST else f"< {below:.2f}"
            deps.tracer.emit(
                AgentRole.SUPERVISOR,
                TraceKind.ROUTE,
                target,
                f"ocr {confidence:.2f} {why} · vision first",
                data={
                    "target": target,
                    "reason": None,
                    "text_attempts": 0,
                    "vision_attempts": 0,
                    "feedback": [],
                    "ocr_confidence": confidence,
                },
            )
        return target

    return after_read


def supervisor_node(deps: AgentDeps) -> Node:
    configured = graph_limits(deps.settings)

    def supervisor(state: ReceiptState) -> ReceiptState:
        limits = _limits(configured, state)
        extraction = state.get("extraction")
        facts = RoutingFacts(
            parsed=extraction is not None,
            valid=state.get("valid", False),
            confidence=None if extraction is None else float(extraction["confidence"]),
            text_attempts=state.get("text_attempts", 0),
            vision_attempts=state.get("vision_attempts", 0),
        )
        decision = route(facts, limits)
        update: ReceiptState = {
            "route": decision.target,
            "review_reason": None if decision.reason is None else decision.reason.value,
        }
        retry = decision.target in RETRY_TARGETS
        # The first vision attempt starts clean, whatever the text attempts said.
        clean = decision.target == "extract_vision" and facts.vision_attempts == 0
        feedback = _feedback(state, limits.feedback) if retry and not clean else []
        if retry:
            update["feedback"] = feedback
        deps.tracer.emit(
            AgentRole.SUPERVISOR,
            TraceKind.ROUTE,
            decision.target,
            _route_summary(decision, facts, limits, feedback),
            data={
                "target": decision.target,
                "reason": update["review_reason"],
                "text_attempts": facts.text_attempts,
                "vision_attempts": facts.vision_attempts,
                "feedback": list[JsonValue](feedback),
            },
        )
        if decision.reason is not None:
            update["review_id"] = _open_review(deps, state, decision.reason)
        return update

    return supervisor


def _feedback(state: ReceiptState, specific: bool) -> list[str]:
    if not specific:
        return [GENERIC_RETRY]
    if state.get("extraction") is None:
        return [UNPARSED_RETRY.format(error=state.get("parse_error") or "unknown error")]
    return [str(issue["message"]) for issue in state.get("issues", [])]


def _open_review(deps: AgentDeps, state: ReceiptState, reason: ReviewReason) -> int:
    receipt_id = state["receipt_id"]
    result = deps.tools.call(
        AgentRole.SUPERVISOR,
        "open_review",
        {
            "receipt_id": receipt_id,
            "reason": reason.value,
            "details": {"issues": list(state.get("issues", []))},
            "thread_id": thread_id(receipt_id),
        },
    )
    return as_int(result["review_id"])


def _route_summary(
    decision: RouteDecision, facts: RoutingFacts, limits: GraphLimits, feedback: list[str]
) -> str:
    if decision.target in RETRY_TARGETS:
        attempt = (
            f"attempt {facts.text_attempts + 1}/{limits.max_text_attempts}"
            if decision.target == "extract_text"
            else f"vision {facts.vision_attempts + 1}/{limits.max_vision_attempts}"
        )
        if not feedback:
            return f"{attempt} · clean prompt"
        if not limits.feedback:
            return f"{attempt} · generic retry"
        return f"{attempt} · feedback {len(feedback)} line{'s' if len(feedback) != 1 else ''}"
    if decision.reason is not None:
        return decision.reason.value
    return "valid"
