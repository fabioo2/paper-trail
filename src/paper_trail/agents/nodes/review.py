"""``review``: wait for a person (LangGraph ``interrupt``); resumed by ``ResolveReview``.

The review item was opened by the supervisor. The answer is the person's draft (DraftPayload
JSON), or ``ACCEPT`` to accept the extraction the graph holds (LangGraph cannot resume with
None); either way it is approved.
"""

from langgraph.types import interrupt

from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node

ACCEPT = "accept"  # the resume value that accepts the extraction as it is


def review_node() -> Node:
    def review(state: ReceiptState) -> ReceiptState:
        answer = interrupt(
            {"review_id": state.get("review_id"), "reason": state.get("review_reason")}
        )
        return {"draft": None if answer == ACCEPT else answer, "approved": True}

    return review
