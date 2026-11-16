"""The compiled receipt graph's nodes and edges (AC-A-12 snapshot; claude-extraction plan TF,
V13, CE-TC-029).

Pinned: ``build_receipt_graph(deps, checkpointer)`` in ``paper_trail.agents.graph`` returns the
compiled LangGraph graph of 9 nodes: ``START -> extract -> validate -> supervisor ->
extract | review | persist``, ``review -> persist -> match -> commit_matches``, then
``enrich -> commit_enrichment -> END`` or, with nothing to enrich, ``END``. ``NODE_NAMES`` is
the tuple of the graph's node names in graph order (what ``process --interrupt-after``
accepts); ``read``, ``extract_text`` and ``extract_vision`` are gone.
"""

from langgraph.checkpoint.memory import InMemorySaver

from paper_trail.agents.graph import NODE_NAMES, AgentDeps, build_receipt_graph
from tests.agent_harness import build_harness
from tests.fakes.reader import ScriptedReader

NODE_ORDER = (
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
NODES = {"__start__", *NODE_ORDER, "__end__"}
EDGES = {
    ("__start__", "extract"),
    ("extract", "validate"),
    ("validate", "supervisor"),
    ("supervisor", "extract"),
    ("supervisor", "review"),
    ("supervisor", "persist"),
    ("review", "persist"),
    ("persist", "match"),
    ("match", "commit_matches"),
    ("commit_matches", "enrich"),
    ("commit_matches", "__end__"),
    ("enrich", "commit_enrichment"),
    ("commit_enrichment", "__end__"),
}


def _deps() -> AgentDeps:
    return build_harness(ScriptedReader()).deps


def test_ce_tc_029_node_and_edge_snapshot() -> None:
    drawable = build_receipt_graph(_deps(), InMemorySaver()).get_graph()

    assert set(drawable.nodes) == NODES
    assert {(edge.source, edge.target) for edge in drawable.edges} == EDGES


def test_ce_tc_029_mermaid_names_every_node_and_no_removed_one() -> None:
    mermaid = build_receipt_graph(_deps(), InMemorySaver()).get_graph().draw_mermaid()

    assert "graph TD" in mermaid
    assert [node for node in NODE_ORDER if node not in mermaid] == []
    assert [node for node in ("read", "extract_text", "extract_vision") if node in mermaid] == []


def test_ce_tc_029_node_names_are_the_graphs_nodes_in_graph_order() -> None:
    assert NODE_NAMES == NODE_ORDER
