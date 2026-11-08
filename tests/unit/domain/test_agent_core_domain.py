"""Domain additions of the agent core (PLAN 3.2 and 5, agent-core plan 2.2).

Pinned (docs/tests/agent-core-A.md): ``Receipt.image_sha256`` and ``LineItem.match_source`` are
new trailing fields defaulting to None, so Stage 1 call sites are unchanged; ``TraceEvent`` is a
frozen dataclass; ``UnsupportedMediaError`` is a ``DomainError`` (415, not 422).
"""

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from paper_trail.domain.enums import AgentRole, MatchSource
from paper_trail.domain.errors import DomainError, DomainValidationError, UnsupportedMediaError
from paper_trail.domain.trace import TraceEvent, TraceKind
from tests.factories import line_item, receipt


def test_agent_roles_are_the_five_of_the_allowlist() -> None:
    """ADR-0013 amendment: AgentRole lives in the domain, values are the lowercase names."""
    assert [role.value for role in AgentRole] == [
        "supervisor",
        "extraction",
        "validation",
        "matching",
        "enrichment",
    ]


def test_match_sources_are_the_matching_order() -> None:
    """PLAN 6: memory, alias, vector, llm, new (stored in line_items.match_source)."""
    assert [source.value for source in MatchSource] == ["memory", "alias", "vector", "llm", "new"]


def test_trace_kinds_serialise_to_lowercase() -> None:
    """Agent-core plan 2.2: NODE, TOOL, LLM, ROUTE, INTERRUPT, ERROR."""
    assert [kind.value for kind in TraceKind] == [
        "node",
        "tool",
        "llm",
        "route",
        "interrupt",
        "error",
        "note",  # real run 2026-11-08: a node's visible note, e.g. a reconciled multi-buy
    ]
    assert json.dumps(TraceKind.LLM) == '"llm"'


def test_receipt_and_line_item_gain_optional_agent_core_fields() -> None:
    """PLAN 3.4: Receipt.image_sha256 and LineItem.match_source default to None."""
    assert receipt().image_sha256 is None
    assert line_item().match_source is None
    stored = receipt(image_sha256="ab" * 32, items=(line_item(match_source=MatchSource.ALIAS),))
    assert stored.image_sha256 == "ab" * 32
    assert stored.items[0].match_source is MatchSource.ALIAS


def test_trace_event_is_frozen() -> None:
    event = TraceEvent(
        seq=1,
        at=datetime(2026, 11, 6, tzinfo=UTC),
        receipt_id=1,
        agent=AgentRole.EXTRACTION,
        kind=TraceKind.TOOL,
        name="ocr_read",
        summary="1,284 chars",
        data={"allowed": True},
        duration_ms=12,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.seq = 2  # type: ignore[misc]


def test_unsupported_media_is_a_domain_error_but_not_a_validation_error() -> None:
    """PLAN 1.5: it maps to 415, not to the 422 of DomainValidationError."""
    assert issubclass(UnsupportedMediaError, DomainError)
    assert not issubclass(UnsupportedMediaError, DomainValidationError)
