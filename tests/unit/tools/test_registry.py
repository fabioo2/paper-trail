"""The tool registry: one declaration per tool, roles enforced in one place (PLAN 7, ADR-0013).

Covers S5-AC-01 (exhaustive role x tool matrix), S5-AC-02 (input and output validated) and the
logging half of AC-B-02 (``tool.denied`` with role and tool).

Pinned (docs/tests/agent-core-A.md and agent-core-B.md):
- ``build_tool_registry(services) -> ToolRegistry``; ``ToolRegistry.specs`` holds every
  ``ToolSpec`` (``name``, ``description``, ``input_model``, ``output_model``, ``handler``,
  ``roles``); ``ToolRegistry.for_role(role) -> RoleToolbox``; ``RoleToolbox.names`` is the
  sorted tuple of the role's tools; ``RoleToolbox.call(name, payload)`` returns the output model.
- A call is checked in this order: the tool exists (``UnknownToolError``), the role may call it
  (``ToolNotPermittedError``, logged as ``tool.denied`` at WARNING with ``role`` and ``tool``,
  whatever the payload), the input validates (``ToolInputError`` naming the field), then the
  handler's output validates against the spec's ``output_model`` (``ToolOutputError``, which is a
  bug in the tool and therefore not a ``ToolError``).
- ``ToolRegistry(specs)`` raises ``ValueError`` for two specs with one name or a spec that no
  role may call.
- The allowlist is PLAN 7's table. The matrix below is generated from the registry, so a tool
  added in slice C or D is checked as soon as it is registered; it needs an entry in
  ``PLAN_ALLOWLIST`` (already there for the planned tools) and one in ``SAMPLE_PAYLOADS``.
- Slice C registers ``product_lookup``, ``product_similarity_search`` and ``save_matches``
  (docs/tests/agent-core-C.md; their behaviour is in ``tests/contract/test_matching_tools.py``).
- Slice D registers ``save_enrichment`` (docs/tests/agent-core-D.md; its behaviour is in
  ``tests/contract/test_enrichment_tools.py``).
"""

import itertools
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from paper_trail.application.payloads import DraftPayload
from paper_trail.domain.enums import AgentRole, ProcessingStage, ReceiptStatus
from paper_trail.domain.models import JsonValue
from paper_trail.ports.tools import (
    ToolError,
    ToolInputError,
    ToolNotPermittedError,
    ToolOutputError,
    UnknownToolError,
)
from paper_trail.tools.models import (
    CheckArithmeticOutput,
    OcrReadInput,
    OcrReadOutput,
)
from paper_trail.tools.registry import ToolRegistry, ToolSpec
from tests.agent_harness import Harness, build_harness
from tests.extractions import enrichment, extraction, without_confidence
from tests.factories import product, receipt, receipt_draft
from tests.fakes.images import fake_png
from tests.fakes.llm import ScriptedLlm
from tests.storage import in_unit
from tests.support import require_id

EXTRACTION = AgentRole.EXTRACTION
VALIDATION = AgentRole.VALIDATION
MATCHING = AgentRole.MATCHING
ENRICHMENT = AgentRole.ENRICHMENT
SUPERVISOR = AgentRole.SUPERVISOR

# PLAN 7 / agent-core plan 2.3: specialists only read; every write is a supervisor tool.
PLAN_ALLOWLIST: dict[str, frozenset[AgentRole]] = {
    "ocr_read": frozenset({EXTRACTION}),
    "check_arithmetic": frozenset({VALIDATION}),
    "product_lookup": frozenset({MATCHING, ENRICHMENT}),
    "product_similarity_search": frozenset({MATCHING}),
    "save_receipt": frozenset({SUPERVISOR}),
    "open_review": frozenset({SUPERVISOR}),
    "save_matches": frozenset({SUPERVISOR}),
    "save_enrichment": frozenset({SUPERVISOR}),
}
WRITE_TOOLS = frozenset({"save_receipt", "open_review", "save_matches", "save_enrichment"})
SLICE_A_TOOLS = frozenset({"ocr_read", "check_arithmetic", "save_receipt", "open_review"})
SLICE_C_TOOLS = frozenset({"product_lookup", "product_similarity_search", "save_matches"})
SLICE_D_TOOLS = frozenset({"save_enrichment"})

REGISTERED = tuple(spec.name for spec in build_harness(ScriptedLlm()).registry.specs)


def _processing_receipt(harness: Harness) -> int:
    stored = receipt(status=ReceiptStatus.PROCESSING, stage=ProcessingStage.READING)
    return require_id(in_unit(harness.uow_factory, lambda uow: uow.receipts.add(stored)).id)


def _ocr_read(harness: Harness) -> dict[str, JsonValue]:
    return {"image_path": harness.images.save(fake_png("matrix")).relative_path}


def _check_arithmetic(_: Harness) -> dict[str, JsonValue]:
    return {"receipt": without_confidence(extraction())}


def _save_receipt(harness: Harness) -> dict[str, JsonValue]:
    draft: JsonValue = DraftPayload.from_draft(receipt_draft()).model_dump(mode="json")
    return {"receipt_id": _processing_receipt(harness), "draft": draft}


def _open_review(harness: Harness) -> dict[str, JsonValue]:
    receipt_id = _processing_receipt(harness)
    return {
        "receipt_id": receipt_id,
        "reason": "validation_failed",
        "details": {"issues": []},
        "thread_id": f"receipt-{receipt_id}",
    }


def _product_lookup(_: Harness) -> dict[str, JsonValue]:
    return {"alias": "KS ORG EGGS 24CT", "store_key": "costco"}


def _product_similarity_search(_: Harness) -> dict[str, JsonValue]:
    return {"text": "ks org eggs 24ct", "k": 3}


def _save_matches(harness: Harness) -> dict[str, JsonValue]:
    stored = receipt()  # COMPLETED, two ITEM lines
    receipt_id = require_id(in_unit(harness.uow_factory, lambda uow: uow.receipts.add(stored)).id)
    decision: dict[str, JsonValue] = {
        "position": 0,
        "source": "new",
        "product_id": None,
        "new_product_name": "Kirkland Organic Eggs 24",
    }
    return {"receipt_id": receipt_id, "decisions": [decision]}


def _save_enrichment(harness: Harness) -> dict[str, JsonValue]:
    stored = product(category=None, tags=())
    product_id = require_id(in_unit(harness.uow_factory, lambda uow: uow.products.add(stored)).id)
    answer = {key: value for key, value in enrichment().items() if key != "confidence"}
    return {
        "product_id": product_id,
        "alias": "KS ORG EGGS 24CT",
        "store_key": "costco",
        "enrichment": answer,
    }


# A valid payload per registered tool, so every allowed pair is shown to succeed.
SAMPLE_PAYLOADS: dict[str, Callable[[Harness], dict[str, JsonValue]]] = {
    "ocr_read": _ocr_read,
    "check_arithmetic": _check_arithmetic,
    "save_receipt": _save_receipt,
    "open_review": _open_review,
    "product_lookup": _product_lookup,
    "product_similarity_search": _product_similarity_search,
    "save_matches": _save_matches,
    "save_enrichment": _save_enrichment,
}

ALLOWED = [
    (role, tool)
    for role, tool in itertools.product(AgentRole, REGISTERED)
    if role in PLAN_ALLOWLIST.get(tool, frozenset())
]
DENIED = [
    (role, tool)
    for role, tool in itertools.product(AgentRole, REGISTERED)
    if role not in PLAN_ALLOWLIST.get(tool, frozenset())
]


@pytest.fixture
def harness() -> Harness:
    return build_harness(ScriptedLlm())


@pytest.fixture
def registry(harness: Harness) -> ToolRegistry:
    return harness.registry


# --- the allowlist --------------------------------------------------------------------------------


def test_s5_ac_01_every_registered_tool_is_planned_with_exactly_its_roles(
    registry: ToolRegistry,
) -> None:
    declared = {spec.name: spec.roles for spec in registry.specs}

    assert set(declared) <= set(PLAN_ALLOWLIST), "a tool that PLAN 7 does not list"
    assert set(declared) >= SLICE_A_TOOLS | SLICE_C_TOOLS | SLICE_D_TOOLS
    assert declared == {name: PLAN_ALLOWLIST[name] for name in declared}


def test_s5_ac_01_only_the_supervisor_writes(registry: ToolRegistry) -> None:
    for spec in registry.specs:
        if spec.name in WRITE_TOOLS:
            assert spec.roles == {SUPERVISOR}, spec.name
        assert spec.description.strip(), spec.name


@pytest.mark.parametrize("role", list(AgentRole))
def test_each_role_sees_exactly_its_tools(registry: ToolRegistry, role: AgentRole) -> None:
    expected = sorted(name for name in REGISTERED if role in PLAN_ALLOWLIST[name])

    assert registry.for_role(role).names == tuple(expected)


@pytest.mark.parametrize(("role", "tool"), ALLOWED)
def test_s5_ac_01_every_allowed_pair_succeeds(
    harness: Harness, registry: ToolRegistry, role: AgentRole, tool: str
) -> None:
    sample = SAMPLE_PAYLOADS.get(tool)
    assert sample is not None, f"add a sample payload for {tool!r} to SAMPLE_PAYLOADS"
    spec = next(spec for spec in registry.specs if spec.name == tool)

    with capture_logs() as logs:
        output = registry.for_role(role).call(tool, sample(harness))

    assert isinstance(output, spec.output_model)
    assert [entry for entry in logs if entry["event"] == "tool.denied"] == []


@pytest.mark.parametrize(("role", "tool"), DENIED)
def test_s5_ac_01_every_other_pair_is_denied_logged_and_raised(
    registry: ToolRegistry, role: AgentRole, tool: str
) -> None:
    """S5-AC-01 / AC-B-02: denied before the input is read (the payload is empty), logged as
    ``tool.denied`` with role and tool, then raised."""
    with capture_logs() as logs, pytest.raises(ToolNotPermittedError) as raised:
        registry.for_role(role).call(tool, {})

    assert str(raised.value) == f"denied: role={role.value} tool={tool}"
    assert (raised.value.role, raised.value.tool) == (role, tool)
    assert isinstance(raised.value, ToolError)
    denied = [entry for entry in logs if entry["event"] == "tool.denied"]
    assert len(denied) == 1
    assert (denied[0]["role"], denied[0]["tool"]) == (role.value, tool)
    assert denied[0]["log_level"] == "warning"


def test_unknown_tools_are_reported(registry: ToolRegistry) -> None:
    with pytest.raises(UnknownToolError, match="no_such_tool"):
        registry.for_role(SUPERVISOR).call("no_such_tool", {})


# --- S5-AC-02: input and output validated ---------------------------------------------------------


def test_s5_ac_02_invalid_input_names_the_field(registry: ToolRegistry) -> None:
    with pytest.raises(ToolInputError, match="image_path"):
        registry.for_role(EXTRACTION).call("ocr_read", {"image_path": 42})
    with pytest.raises(ToolInputError, match="image_path"):
        registry.for_role(EXTRACTION).call("ocr_read", {})


def test_s5_ac_02_unexpected_input_fields_are_refused(registry: ToolRegistry) -> None:
    with pytest.raises(ToolInputError, match="sneaky"):
        registry.for_role(EXTRACTION).call("ocr_read", {"image_path": "a.jpg", "sneaky": True})


def _one_tool_registry(handler: Callable[[OcrReadInput], BaseModel]) -> ToolRegistry:
    """``ocr_read`` declared with a handler that returns something else than its output."""
    return ToolRegistry(
        (
            ToolSpec[OcrReadInput, Any](
                name="ocr_read",
                description="A tool whose handler misbehaves.",
                input_model=OcrReadInput,
                output_model=OcrReadOutput,
                handler=handler,
                roles=frozenset({EXTRACTION}),
            ),
        )
    )


def test_s5_ac_02_output_that_does_not_validate_is_an_output_error() -> None:
    invalid: dict[str, Any] = {"text": 5, "mean_confidence": "high", "masked_pans": 0}
    broken = _one_tool_registry(lambda _: OcrReadOutput.model_construct(**invalid))

    with pytest.raises(ToolOutputError, match="ocr_read") as raised:
        broken.for_role(EXTRACTION).call("ocr_read", {"image_path": "a.jpg"})

    assert not isinstance(raised.value, ToolError)  # a bug in the tool, not the caller's fault


def test_s5_ac_02_output_of_the_wrong_model_is_an_output_error() -> None:
    wrong = _one_tool_registry(lambda _: CheckArithmeticOutput(ok=True, issues=[]))

    with pytest.raises(ToolOutputError, match="ocr_read"):
        wrong.for_role(EXTRACTION).call("ocr_read", {"image_path": "a.jpg"})


# --- the registry itself ------------------------------------------------------------------------


def test_two_tools_with_one_name_are_refused(registry: ToolRegistry) -> None:
    (first, *_) = registry.specs

    with pytest.raises(ValueError, match=first.name):
        ToolRegistry((first, first))


def test_a_tool_no_role_may_call_is_refused() -> None:
    with pytest.raises(ValueError, match="ocr_read"):
        ToolRegistry(
            (
                ToolSpec(
                    name="ocr_read",
                    description="Nobody may call this.",
                    input_model=OcrReadInput,
                    output_model=OcrReadOutput,
                    handler=lambda _: OcrReadOutput(
                        text="", mean_confidence=None, rotation_degrees=0, masked_pans=0
                    ),
                    roles=frozenset(),
                ),
            )
        )
