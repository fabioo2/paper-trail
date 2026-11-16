"""The receipt graph (agent-core plan 2.4, ADR-0008).

    START -> extract -> validate -> supervisor
    supervisor -> extract | review | persist   (the retry model, then a person)
    review -> persist -> match -> commit_matches
    commit_matches -> enrich -> commit_enrichment -> END   (new products, GRAPH__ENRICHMENT)
    commit_matches -> END                                  (nothing to enrich)

Every node is wrapped by ``traced_node`` with its agent.
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.nodes.commit import commit_enrichment_node, commit_matches_node
from paper_trail.agents.nodes.enrichment import enrich_node
from paper_trail.agents.nodes.extraction import extract_node
from paper_trail.agents.nodes.matching import match_node
from paper_trail.agents.nodes.persist import persist_node
from paper_trail.agents.nodes.review import review_node
from paper_trail.agents.nodes.supervisor import supervisor_node
from paper_trail.agents.nodes.validation import validate_node
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node, traced_node
from paper_trail.domain.enums import AgentRole

__all__ = ["NODE_NAMES", "AgentDeps", "ReceiptGraph", "build_receipt_graph"]

# Every node, in graph order: what ``process --interrupt-after`` accepts.
NODE_NAMES = (
    "extract",
    "validate",
    "supervisor",
    "review",
    "persist",
    "match",
    "commit_matches",
    "enrich",
    "commit_enrichment",
)

type ReceiptGraph = CompiledStateGraph[ReceiptState, None, ReceiptState, ReceiptState]


def _after_supervisor(state: ReceiptState) -> str:
    return state["route"]


def build_receipt_graph(deps: AgentDeps, checkpointer: BaseCheckpointSaver[Any]) -> ReceiptGraph:
    enrichment = deps.settings.graph.enrichment

    def after_commit_matches(state: ReceiptState) -> str:
        return "enrich" if enrichment and state.get("created") else END

    nodes: dict[str, tuple[AgentRole, Node]] = {
        "extract": (AgentRole.EXTRACTION, extract_node(deps)),
        "validate": (AgentRole.VALIDATION, validate_node(deps)),
        "supervisor": (AgentRole.SUPERVISOR, supervisor_node(deps)),
        "review": (AgentRole.SUPERVISOR, review_node()),
        "persist": (AgentRole.SUPERVISOR, persist_node(deps)),
        "match": (AgentRole.MATCHING, match_node(deps)),
        "commit_matches": (AgentRole.SUPERVISOR, commit_matches_node(deps)),
        "enrich": (AgentRole.ENRICHMENT, enrich_node(deps)),
        "commit_enrichment": (AgentRole.SUPERVISOR, commit_enrichment_node(deps)),
    }
    builder = StateGraph(ReceiptState)
    for name in NODE_NAMES:
        agent, node = nodes[name]
        builder.add_node(name, traced_node(deps.tracer, agent, name, node))
    builder.add_edge(START, "extract")
    builder.add_edge("extract", "validate")
    builder.add_edge("validate", "supervisor")
    builder.add_conditional_edges("supervisor", _after_supervisor, ["extract", "review", "persist"])
    builder.add_edge("review", "persist")
    builder.add_edge("persist", "match")
    builder.add_edge("match", "commit_matches")
    builder.add_conditional_edges("commit_matches", after_commit_matches, ["enrich", END])
    builder.add_edge("enrich", "commit_enrichment")
    builder.add_edge("commit_enrichment", END)
    return builder.compile(checkpointer=checkpointer)
