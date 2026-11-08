"""The supervisor's writes of what the specialists decided.

``commit_matches`` writes the matching agent's decisions with ``save_matches``: one call per
run, also with no decisions. The tool links the lines, creates the new products with their
vectors, remembers the aliases and records the prices in one unit of work, and completes the
receipt. It is idempotent, so a run resumed after a crash here repeats it safely. The lines it
linked to new products are what the enrichment agent describes next.

``commit_enrichment`` saves each product the enrichment agent described with
``save_enrichment``, in the order they were asked about. The enrichment requests all come
first: with one model loaded at a time, the text model's calls and then the embedding model's
cost one model switch, not one per product.
"""

from typing import Any

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.json_values import as_dict, as_list, as_str
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
        lines = [as_dict(line) for line in as_list(result["lines"])]
        created: list[dict[str, Any]] = [
            {"position": line["position"], "product_id": line["product_id"]}
            for line in lines
            if as_str(line["source"]) == MatchSource.NEW.value
        ]
        return {"status": ReceiptStatus.COMPLETED.value, "created": created}

    return commit_matches


def commit_enrichment_node(deps: AgentDeps) -> Node:
    def commit_enrichment(state: ReceiptState) -> ReceiptState:
        for enrichment in state.get("enrichments", []):
            deps.tools.call(AgentRole.SUPERVISOR, "save_enrichment", enrichment)
        return {}

    return commit_enrichment
