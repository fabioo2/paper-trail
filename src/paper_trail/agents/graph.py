"""The receipt graph (agent-core plan 2.4, ADR-0008).

    START -> read -> extract_text -> validate -> supervisor
    read -> extract_vision                     (a poor OCR read of a photo: vision first)
    START -> extract_text                      (the OCR text is already known)
    supervisor -> extract_text | extract_vision | review | persist
    extract_vision -> validate
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
from paper_trail.agents.nodes.extraction import extract_text_node, extract_vision_node
from paper_trail.agents.nodes.matching import match_node
from paper_trail.agents.nodes.persist import persist_node
from paper_trail.agents.nodes.read import read_node
from paper_trail.agents.nodes.review import review_node
from paper_trail.agents.nodes.supervisor import first_attempt_after_read, supervisor_node
from paper_trail.agents.nodes.validation import validate_node
from paper_trail.agents.state import ReceiptState
from paper_trail.agents.tracing import Node, traced_node
from paper_trail.domain.enums import AgentRole

__all__ = ["NODE_NAMES", "AgentDeps", "ReceiptGraph", "build_receipt_graph"]

# Every node, in graph order: what ``process --interrupt-after`` accepts.
NODE_NAMES = (
    "read",
    "extract_text",
    "extract_vision",
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


def _after_start(state: ReceiptState) -> str:
    return "extract_text" if state.get("ocr_text") is not None else "read"


def _after_supervisor(state: ReceiptState) -> str:
    return state["route"]


def build_receipt_graph(deps: AgentDeps, checkpointer: BaseCheckpointSaver[Any]) -> ReceiptGraph:
    enrichment = deps.settings.graph.enrichment

    def after_commit_matches(state: ReceiptState) -> str:
        return "enrich" if enrichment and state.get("created") else END

    nodes: dict[str, tuple[AgentRole, Node]] = {
        "read": (AgentRole.EXTRACTION, read_node(deps)),
        "extract_text": (AgentRole.EXTRACTION, extract_text_node(deps)),
        "extract_vision": (AgentRole.EXTRACTION, extract_vision_node(deps)),
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
    builder.add_conditional_edges(START, _after_start, ["read", "extract_text"])
    builder.add_conditional_edges(
        "read", first_attempt_after_read(deps), ["extract_text", "extract_vision"]
    )
    builder.add_edge("extract_text", "validate")
    builder.add_edge("extract_vision", "validate")
    builder.add_edge("validate", "supervisor")
    builder.add_conditional_edges(
        "supervisor", _after_supervisor, ["extract_text", "extract_vision", "review", "persist"]
    )
    builder.add_edge("review", "persist")
    builder.add_edge("persist", "match")
    builder.add_edge("match", "commit_matches")
    builder.add_conditional_edges("commit_matches", after_commit_matches, ["enrich", END])
    builder.add_edge("enrich", "commit_enrichment")
    builder.add_edge("commit_enrichment", END)
    return builder.compile(checkpointer=checkpointer)
