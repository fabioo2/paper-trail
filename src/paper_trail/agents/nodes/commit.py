"""The supervisor's writes of what the specialists decided.

``commit_matches`` writes the matching agent's decisions with ``save_matches``: one call per
run, also with no decisions. The tool links the lines, creates the new products and their
codes, remembers the aliases of uncoded lines and records the prices in one unit of work, and
completes the receipt. It is idempotent, so a run resumed after a crash
here repeats it safely. The lines it linked to new products are what the enrichment agent
describes next, each with the catalog entry matching named it from (none for a printed-text
product).

``commit_enrichment`` writes each description the enrichment agent made with
``describe_from_catalog``, in line order, then has the enricher look up or guess every product
the receipt created (claude-extraction plan D3, V8): the network calls happen outside any write
transaction, and each product is written in its own unit, so a crash leaves the products
already done enriched and a resumed run finishes the rest without asking again. A failure
never fails the receipt, which ``commit_matches`` completed. One NOTE says how many products
were enriched, with each product's outcome.
"""

from typing import Any

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.json_values import as_dict, as_int, as_list, as_str
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node
from paper_trail.domain.enums import AgentRole, MatchSource, ReceiptStatus
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.enrichment import EnrichOutcome, EnrichTarget

NOTE = "enrichment"


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
        targets = [EnrichTarget(**target) for target in state.get("targets", [])]
        if deps.enricher is None or not targets:
            return {}
        done = deps.enricher.enrich_all(targets)
        enriched = sum(
            result.outcome in {EnrichOutcome.LOOKED_UP, EnrichOutcome.GUESSED} for result in done
        )
        deps.tracer.emit(
            AgentRole.SUPERVISOR,
            TraceKind.NOTE,
            NOTE,
            f"{enriched} of {len(done)} enriched",
            data={
                "enriched": enriched,
                "products": [
                    {
                        "product_id": result.product_id,
                        "outcome": result.outcome.value,
                        "written": {key: source.value for key, source in result.written.items()},
                        "lookups": [
                            {"source": event.source, "result": event.result}
                            for event in result.lookups
                        ],
                        "guessed": result.guessed,
                    }
                    for result in done
                ],
            },
        )
        return {}

    return commit_enrichment
