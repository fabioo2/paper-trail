"""``persist`` (supervisor): save the receipt through the ``save_receipt`` tool.

What is saved is the person's draft after a review, else the valid extraction as a draft
(``saved_draft``), approved either way (a person's draft even if forced through). The tool
leaves the receipt PROCESSING/MATCHING: ``commit_matches`` completes it once its lines are
linked. A discount that stays at receipt level is named in a NOTE, so none goes unexplained.
"""

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.drafts import saved_draft
from paper_trail.agents.json_values import as_dict, as_list, as_str
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node
from paper_trail.application.payloads import DraftPayload
from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.domain.trace import TraceKind


def persist_node(deps: AgentDeps) -> Node:
    def persist(state: ReceiptState) -> ReceiptState:
        result = deps.tools.call(
            AgentRole.SUPERVISOR,
            "save_receipt",
            {
                "receipt_id": state["receipt_id"],
                "draft": DraftPayload.from_draft(saved_draft(deps, state)).model_dump(mode="json"),
                "force": state.get("approved", False),
            },
        )
        _note_receipt_level(deps, as_list(result["discounts_receipt_level"]))
        return {"saved": True, "status": as_str(result["status"])}

    return persist


def _note_receipt_level(deps: AgentDeps, discounts: list[JsonValue]) -> None:
    if not discounts:
        return
    named = "; ".join(
        f"pos {entry['position']} `{entry['raw_text']}` ({entry['reason']})"
        for entry in map(as_dict, discounts)
    )
    deps.tracer.emit(
        AgentRole.SUPERVISOR,
        TraceKind.NOTE,
        "discounts",
        f"kept at receipt level: {named}",
        data={"receipt_level": discounts},
    )
