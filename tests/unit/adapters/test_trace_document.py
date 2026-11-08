"""The JSON trace format ``paper-trail.trace/v1`` (AC-A-09, agent-core plan 4.3).

Pinned (docs/tests/agent-core-A.md): the document is the Pydantic model
``paper_trail.adapters.trace.json_file.TraceDocument`` (``extra="forbid"`` at every level); its
JSON schema is committed as ``src/paper_trail/adapters/trace/trace.schema.json`` (the plan put it
under ``agents/``; it describes the adapter's output, so it lives beside it) and must not drift.
The CLI suites validate the written files against the model.
"""

import json

from paper_trail.adapters.trace.json_file import TraceDocument
from tests.support import REPO_ROOT

SCHEMA_FILE = REPO_ROOT / "src" / "paper_trail" / "adapters" / "trace" / "trace.schema.json"
TOP_LEVEL = {
    "schema",
    "run",
    "receipt_id",
    "source_name",
    "image_sha256",
    "started_at",
    "finished_at",
    "config",
    "outcome",
    "totals",
    "events",
}


def test_ac_a_09_schema_file_equals_the_model_schema() -> None:
    assert json.loads(SCHEMA_FILE.read_text(encoding="utf-8")) == TraceDocument.model_json_schema()


def test_ac_a_09_document_fields_follow_the_plan() -> None:
    """Agent-core plan 4.3: top-level fields, config, outcome, totals and event fields."""
    schema = TraceDocument.model_json_schema()
    definitions = schema["$defs"]

    def fields(name: str) -> set[str]:
        return set(definitions[name]["properties"])

    assert set(schema["properties"]) == TOP_LEVEL
    assert schema["additionalProperties"] is False
    assert fields("TraceConfig") == {
        "models",
        "prompt_versions",
        "examples",
        "feedback",
        "max_text_attempts",
        "max_vision_attempts",
        "enrichment",  # slice D: GRAPH__ENRICHMENT (docs/tests/agent-core-D.md)
        "transport",
    }
    assert fields("TraceOutcome") == {"status", "review_id", "review_reason", "resumed_from"}
    assert fields("TraceTotals") == {
        "llm_calls",
        "prompt_tokens",
        "completion_tokens",
        "tool_calls",
        "tool_denied",
        "text_attempts",
        "vision_attempts",
    }
    assert fields("TraceEventOut") == {
        "seq",
        "at",
        "agent",
        "kind",
        "name",
        "summary",
        "duration_ms",
        "data",
    }
    for name in ("TraceConfig", "TraceOutcome", "TraceTotals", "TraceEventOut"):
        assert definitions[name]["additionalProperties"] is False, name
