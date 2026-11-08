"""``validate`` (validation role): the receipt is checked in code, never by a model.

The totals through the ``check_arithmetic`` tool; then completeness (``check_completeness``,
pure domain code): a receipt without a store name or a purchase date is not valid either, and
its issues route and feed back exactly like arithmetic ones (ADR-0017). An answer that did not
parse has nothing to check and is simply not valid.
"""

from typing import Any

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.drafts import extraction_draft
from paper_trail.agents.json_values import as_bool, as_list
from paper_trail.agents.schemas import ExtractionOutput
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node
from paper_trail.application.payloads import IssuePayload
from paper_trail.domain.enums import AgentRole
from paper_trail.domain.rules.completeness import check_completeness


def validate_node(deps: AgentDeps) -> Node:
    def validate(state: ReceiptState) -> ReceiptState:
        extraction = state.get("extraction")
        if extraction is None:
            return {"valid": False, "issues": []}
        output = ExtractionOutput.model_validate(extraction)
        result = deps.tools.call(
            AgentRole.VALIDATION, "check_arithmetic", {"receipt": output.receipt_json()}
        )
        issues: list[dict[str, Any]] = [
            issue for issue in as_list(result["issues"]) if isinstance(issue, dict)
        ]
        draft = extraction_draft(deps, state)
        missing = check_completeness(draft.store_name, output.purchased_on, draft.purchased_on)
        issues += [IssuePayload.of(issue).model_dump(mode="json") for issue in missing]
        return {"valid": as_bool(result["ok"]) and not missing, "issues": issues}

    return validate
