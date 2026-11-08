"""The compiled receipt graph's nodes and edges (AC-A-12 snapshot, agent-core plan 2.4).

Pinned (docs/tests/agent-core-A.md): ``build_receipt_graph(deps, checkpointer)`` in
``paper_trail.agents.graph`` returns the compiled LangGraph graph. Slice A has no
``extract_vision`` node (slice D adds it and updates this snapshot). ``START`` branches to
``read`` or, when OCR text is already in the state, straight to ``extract_text``.

Slice C (docs/tests/agent-core-C.md): ``persist -> match -> commit_matches -> END``, and
``paper_trail.agents.graph.NODE_NAMES`` is the tuple of the graph's node names (what
``process --interrupt-after`` accepts).

Slice D (docs/tests/agent-core-D.md): ``supervisor -> extract_vision -> validate``, and
``commit_matches -> enrich -> commit_enrichment -> END`` or, with nothing to enrich,
``commit_matches -> END``. ``NODE_NAMES`` is in graph order.

Extraction accuracy (docs/tests/extraction-accuracy.md, EA-AC-14): ``read -> extract_text``
becomes a conditional edge ``read -> extract_text | extract_vision``; nothing else changes.
"""

from langgraph.checkpoint.memory import InMemorySaver

from paper_trail.agents.graph import NODE_NAMES, AgentDeps, build_receipt_graph
from tests.agent_harness import build_harness
from tests.fakes.llm import ScriptedLlm

NODES = {
    "__start__",
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
    "__end__",
}
EDGES = {
    ("__start__", "read"),
    ("__start__", "extract_text"),
    ("read", "extract_text"),
    ("read", "extract_vision"),  # extraction accuracy: vision first on a poor OCR read
    ("extract_text", "validate"),
    ("validate", "supervisor"),
    ("supervisor", "extract_text"),
    ("supervisor", "extract_vision"),
    ("extract_vision", "validate"),
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
    harness = build_harness(ScriptedLlm())
    return harness.deps


def test_ac_a_12_node_and_edge_snapshot() -> None:
    """AC-A-12: the compiled graph has exactly the planned nodes and edges."""
    drawable = build_receipt_graph(_deps(), InMemorySaver()).get_graph()

    assert set(drawable.nodes) == NODES
    assert {(edge.source, edge.target) for edge in drawable.edges} == EDGES


def test_ac_a_12_mermaid_names_every_node() -> None:
    """AC-A-12: draw_mermaid() renders the same graph (what `paper-trail graph` prints)."""
    mermaid = build_receipt_graph(_deps(), InMemorySaver()).get_graph().draw_mermaid()

    assert "graph TD" in mermaid
    for node in NODES - {"__start__", "__end__"}:
        assert node in mermaid


def test_node_names_are_the_graphs_nodes() -> None:
    """``--interrupt-after`` accepts exactly these (slice C)."""
    assert set(NODE_NAMES) == NODES - {"__start__", "__end__"}
    assert len(NODE_NAMES) == len(set(NODE_NAMES))


def test_node_names_are_in_graph_order() -> None:
    """Slice D: the help of ``--interrupt-after`` lists them as a receipt goes through."""
    assert NODE_NAMES == (
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
