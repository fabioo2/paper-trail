"""``review``: wait for a person (LangGraph ``interrupt``); resumed by ``ResolveReview``.

The review item was opened by the supervisor. The answer is the person's draft (DraftPayload
JSON), or None to accept the extraction the graph holds; either way it is approved.
"""

from langgraph.types import interrupt

from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node


def review_node() -> Node:
    def review(state: ReceiptState) -> ReceiptState:
        answer = interrupt(
            {"review_id": state.get("review_id"), "reason": state.get("review_reason")}
        )
        return {"draft": answer, "approved": True}

    return review
