"""``supervisor``: routes deterministically (``route``) and owns the writes (ADR-0008,
claude-extraction plan TF).

After ``validate``: a valid reading is saved (``persist``); an invalid one is read again on the
retry model while attempts are left (``GRAPH__MAX_ATTEMPTS``), with feedback: the validation
issues verbatim, or with feedback off (experiment E1's control arm) one generic line; then it
goes to a person (``review``). Sending a receipt to review opens the review item here, through
the supervisor's ``open_review`` tool, so that the ``review`` node only waits for a person.
"""

from typing import Literal

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.json_values import as_int
from paper_trail.agents.state import ReceiptState, thread_id
from paper_trail.agents.tracing import Node
from paper_trail.config import ClaudeSettings
from paper_trail.domain.enums import AgentRole, ReviewReason
from paper_trail.domain.models import JsonValue
from paper_trail.domain.trace import TraceKind

GENERIC_RETRY = "The previous answer was inconsistent. Read the receipt again."
UNPARSED_RETRY = "The previous answer could not be read: {error}"

type Escalation = Literal["extract", "review", "persist"]


def attempt_model(settings: ClaudeSettings, attempt: int) -> str:
    """The model of attempt ``attempt`` (1-based): ``extract_model`` first, ``retry_model``
    for the retry (claude-extraction plan TF)."""
    return settings.extract_model if attempt <= 1 else settings.retry_model


def escalation_route(*, valid: bool, attempts: int, max_attempts: int) -> Escalation:
    """After ``validate``: ``persist`` when the latest reading is valid; else ``extract`` again
    while ``attempts < max_attempts`` (``GRAPH__MAX_ATTEMPTS``, the retry gets the failures as
    feedback); else ``review`` (reason ``validation_failed``)."""
    if valid:
        return "persist"
    return "extract" if attempts < max_attempts else "review"


def supervisor_node(deps: AgentDeps) -> Node:
    graph = deps.settings.graph

    def supervisor(state: ReceiptState) -> ReceiptState:
        attempts = state.get("attempts", 0)
        target = escalation_route(
            valid=state.get("valid", False), attempts=attempts, max_attempts=graph.max_attempts
        )
        reason = _reason(state) if target == "review" else None
        update: ReceiptState = {
            "route": target,
            "review_reason": None if reason is None else reason.value,
        }
        feedback = _feedback(state, specific=graph.feedback) if target == "extract" else []
        if target == "extract":
            update["feedback"] = feedback
        deps.tracer.emit(
            AgentRole.SUPERVISOR,
            TraceKind.ROUTE,
            target,
            _route_summary(target, reason, attempts, graph.max_attempts, feedback),
            data={
                "target": target,
                "reason": update["review_reason"],
                "attempts": attempts,
                "feedback": list[JsonValue](feedback),
            },
        )
        if reason is not None:
            update["review_id"] = _open_review(deps, state, reason)
        return update

    return supervisor


def _reason(state: ReceiptState) -> ReviewReason:
    """Why a person must look: the reading never parsed, or it does not add up."""
    if state.get("extraction") is None:
        return ReviewReason.EXTRACTION_FAILED
    return ReviewReason.VALIDATION_FAILED


def _feedback(state: ReceiptState, *, specific: bool) -> list[str]:
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
    target: Escalation,
    reason: ReviewReason | None,
    attempts: int,
    max_attempts: int,
    feedback: list[str],
) -> str:
    if target == "extract":
        attempt = f"attempt {attempts + 1}/{max_attempts}"
        if feedback == [GENERIC_RETRY]:
            return f"{attempt} · generic retry"
        return f"{attempt} · feedback {len(feedback)} line{'s' if len(feedback) != 1 else ''}"
    if reason is not None:
        return reason.value
    return "valid"
