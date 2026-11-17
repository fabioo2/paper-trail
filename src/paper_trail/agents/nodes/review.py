"""``review``: wait for a person (LangGraph ``interrupt``); resumed by ``ResolveReview``.

The review item was opened by the supervisor. The answer is the person's draft,
``{"draft": <DraftPayload JSON>, "attribute_discounts": bool}`` (whether the discount rule links
its unlinked discounts, or the links it was given are kept), or ``ACCEPT`` to accept the
extraction the graph holds (LangGraph cannot resume with None); either way it is approved.
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
        if answer == ACCEPT:
            return {"draft": None, "approved": True}
        return {
            "draft": answer["draft"],
            "attribute_discounts": answer["attribute_discounts"],
            "approved": True,
        }

    return review
