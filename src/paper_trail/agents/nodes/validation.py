"""``validate`` (validation role): the reading is checked in code, never by a model
(claude-extraction plan E2, ADR-0017).

``check_reading`` (each item's regular price, discount and price paid; weighed items; the
receipt's sums), then completeness (``check_completeness``): a receipt without a store name
or a purchase date is not valid either, and its issues route and feed back exactly like
arithmetic ones. Both read the answer in cents only: no draft is built here, so a reading that
does not add up is reported, never a crash. An answer that did not parse has nothing to check
and is simply not valid.
"""

from typing import Any

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.drafts import extraction_reading
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node
from paper_trail.application.payloads import IssuePayload
from paper_trail.domain.rules.completeness import check_completeness
from paper_trail.domain.rules.dates import parse_receipt_date
from paper_trail.domain.rules.readings import check_reading


def validate_node(deps: AgentDeps) -> Node:
    def validate(state: ReceiptState) -> ReceiptState:
        if state.get("extraction") is None:
            return {"valid": False, "issues": []}
        reading = extraction_reading(state)
        date_text = reading.purchased_on
        purchased_on = (
            None if date_text is None else parse_receipt_date(date_text, deps.clock.now().date())
        )
        found = [
            *check_reading(reading).issues,
            *check_completeness(reading.store_name, date_text, purchased_on),
        ]
        issues: list[dict[str, Any]] = [
            IssuePayload.of(issue).model_dump(mode="json") for issue in found
        ]
        return {"valid": not issues, "issues": issues}

    return validate
