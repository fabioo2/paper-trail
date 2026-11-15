"""The supervisor's writes of what the specialists decided.

``commit_matches`` writes the matching agent's decisions with ``save_matches``: one call per
run, also with no decisions. The tool links the lines, creates the new products (a confirmed one
with its vector) and their codes, remembers the aliases of uncoded lines and records the prices
in one unit of work, and completes the receipt. It is idempotent, so a run resumed after a crash
here repeats it safely. The lines it linked to new products are what the enrichment agent
describes next, each with the catalog entry matching named it from (none for a printed-text
product).

``commit_enrichment`` writes each description the enrichment agent made with
``describe_from_catalog``, in line order.
"""

from typing import Any

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.json_values import as_dict, as_int, as_list, as_str
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node
from paper_trail.domain.enums import AgentRole, MatchSource, ReceiptStatus


def commit_matches_node(deps: AgentDeps) -> Node:
    def commit_matches(state: ReceiptState) -> ReceiptState:
        result = deps.tools.call(
            AgentRole.SUPERVISOR,
            "save_matches",
            {"receipt_id": state["receipt_id"], "decisions": list(state.get("matches", []))},
        )
        entries = {
            int(decision["position"]): decision.get("catalog")
            for decision in state.get("matches", [])
        }
        lines = [as_dict(line) for line in as_list(result["lines"])]
        created: list[dict[str, Any]] = [
            {
                "position": line["position"],
                "product_id": line["product_id"],
                "catalog": entries.get(as_int(line["position"])),
            }
            for line in lines
            if as_str(line["source"]) == MatchSource.NEW.value
        ]
        return {"status": ReceiptStatus.COMPLETED.value, "created": created}

    return commit_matches


def commit_enrichment_node(deps: AgentDeps) -> Node:
    def commit_enrichment(state: ReceiptState) -> ReceiptState:
        for description in state.get("enrichments", []):
            deps.tools.call(AgentRole.SUPERVISOR, "describe_from_catalog", description)
        return {}

    return commit_enrichment
