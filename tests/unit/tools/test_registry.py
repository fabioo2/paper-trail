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
- Slice C registers ``product_lookup`` and ``save_matches`` (docs/tests/agent-core-C.md; their
  behaviour is in ``tests/contract/test_matching_tools.py``).
- Claude extraction (CE-TC-034, plan V2) removes ``ocr_read`` and ``product_similarity_search``:
  both are unknown tools now, for every role; each role's list is pinned literally.
- Slice D registered ``save_enrichment``; product codes (barcode-lookup PR 1a, PC-TC-43) remove
  it (``UnknownToolError``) and register ``catalog_lookup`` (MATCHING, ENRICHMENT, read-only) and
  ``describe_from_catalog`` (SUPERVISOR, a write); their behaviour is in
  ``tests/contract/test_catalog_tools.py``.
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
    CheckArithmeticInput,
    CheckArithmeticOutput,
    OpenReviewOutput,
)
from paper_trail.tools.registry import ToolRegistry, ToolSpec
from tests.agent_harness import Harness, build_harness
from tests.extractions import extraction, without_confidence
from tests.factories import product, receipt, receipt_draft
from tests.fakes.reader import ScriptedReader
from tests.storage import in_unit
from tests.support import require_id

EXTRACTION = AgentRole.EXTRACTION
VALIDATION = AgentRole.VALIDATION
MATCHING = AgentRole.MATCHING
ENRICHMENT = AgentRole.ENRICHMENT
SUPERVISOR = AgentRole.SUPERVISOR

# PLAN 7 / agent-core plan 2.3: specialists only read; every write is a supervisor tool.
PLAN_ALLOWLIST: dict[str, frozenset[AgentRole]] = {
    "check_arithmetic": frozenset({VALIDATION}),
    "product_lookup": frozenset({MATCHING, ENRICHMENT}),
    "save_receipt": frozenset({SUPERVISOR}),
    "open_review": frozenset({SUPERVISOR}),
    "save_matches": frozenset({SUPERVISOR}),
    "catalog_lookup": frozenset({MATCHING, ENRICHMENT}),
    "describe_from_catalog": frozenset({SUPERVISOR}),
}
WRITE_TOOLS = frozenset({"save_receipt", "open_review", "save_matches", "describe_from_catalog"})
SLICE_A_TOOLS = frozenset({"check_arithmetic", "save_receipt", "open_review"})
SLICE_C_TOOLS = frozenset({"product_lookup", "save_matches"})
CATALOG_TOOLS = frozenset({"catalog_lookup", "describe_from_catalog"})

REGISTERED = tuple(spec.name for spec in build_harness(ScriptedReader()).registry.specs)


def _processing_receipt(harness: Harness) -> int:
    stored = receipt(status=ReceiptStatus.PROCESSING, stage=ProcessingStage.READING)
    return require_id(in_unit(harness.uow_factory, lambda uow: uow.receipts.add(stored)).id)


def _check_arithmetic(_: Harness) -> dict[str, JsonValue]:
    return _check_arithmetic_payload()


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


def _catalog_lookup(_: Harness) -> dict[str, JsonValue]:
    return {"raw_text": "1362394 CHOC PISTACH", "store_key": "costco"}


def _describe_from_catalog(harness: Harness) -> dict[str, JsonValue]:
    stored = product(category=None, tags=())
    product_id = require_id(in_unit(harness.uow_factory, lambda uow: uow.products.add(stored)).id)
    return {
        "product_id": product_id,
        "fields": {"category": "produce"},
        "sources": {"category": "plu"},
        "alias": "3101 TST APPLE MRJ",
        "store_key": "loblaws",
    }


# A valid payload per registered tool, so every allowed pair is shown to succeed.
SAMPLE_PAYLOADS: dict[str, Callable[[Harness], dict[str, JsonValue]]] = {
    "check_arithmetic": _check_arithmetic,
    "save_receipt": _save_receipt,
    "open_review": _open_review,
    "product_lookup": _product_lookup,
    "save_matches": _save_matches,
    "catalog_lookup": _catalog_lookup,
    "describe_from_catalog": _describe_from_catalog,
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
    return build_harness(ScriptedReader())


@pytest.fixture
def registry(harness: Harness) -> ToolRegistry:
    return harness.registry


# --- the allowlist --------------------------------------------------------------------------------


def test_s5_ac_01_every_registered_tool_is_planned_with_exactly_its_roles(
    registry: ToolRegistry,
) -> None:
    declared = {spec.name: spec.roles for spec in registry.specs}

    assert set(declared) <= set(PLAN_ALLOWLIST), "a tool that PLAN 7 does not list"
    assert set(declared) >= SLICE_A_TOOLS | SLICE_C_TOOLS | CATALOG_TOOLS
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


# CE-TC-034: the lists after Claude extraction, literally (no OCR tool, no similarity search).
ROLE_TOOLS: dict[AgentRole, tuple[str, ...]] = {
    EXTRACTION: (),
    VALIDATION: ("check_arithmetic",),
    MATCHING: ("catalog_lookup", "product_lookup"),
    ENRICHMENT: ("catalog_lookup", "product_lookup"),
    SUPERVISOR: ("describe_from_catalog", "open_review", "save_matches", "save_receipt"),
}


def test_ce_tc_034_each_roles_tool_list_is_exactly_the_planned_one(
    registry: ToolRegistry,
) -> None:
    assert {role: registry.for_role(role).names for role in AgentRole} == ROLE_TOOLS


@pytest.mark.parametrize(
    ("role", "tool"),
    [
        *[(role, "ocr_read") for role in AgentRole],
        *[(role, "product_similarity_search") for role in AgentRole],
        (SUPERVISOR, "no_such_tool"),
        (SUPERVISOR, "save_enrichment"),
    ],
)
def test_unknown_tools_are_reported(registry: ToolRegistry, role: AgentRole, tool: str) -> None:
    with pytest.raises(UnknownToolError, match=tool):
        registry.for_role(role).call(tool, {})


# --- S5-AC-02: input and output validated ---------------------------------------------------------


def test_s5_ac_02_invalid_input_names_the_field(registry: ToolRegistry) -> None:
    with pytest.raises(ToolInputError, match="receipt"):
        registry.for_role(VALIDATION).call("check_arithmetic", {"receipt": 42})
    with pytest.raises(ToolInputError, match="receipt"):
        registry.for_role(VALIDATION).call("check_arithmetic", {})


def test_s5_ac_02_unexpected_input_fields_are_refused(registry: ToolRegistry) -> None:
    payload = {**_check_arithmetic_payload(), "sneaky": True}
    with pytest.raises(ToolInputError, match="sneaky"):
        registry.for_role(VALIDATION).call("check_arithmetic", payload)


def _check_arithmetic_payload() -> dict[str, JsonValue]:
    return {"receipt": without_confidence(extraction())}


def _one_tool_registry(handler: Callable[[CheckArithmeticInput], BaseModel]) -> ToolRegistry:
    """``check_arithmetic`` declared with a handler that returns something else than its
    output."""
    return ToolRegistry(
        (
            ToolSpec[CheckArithmeticInput, Any](
                name="check_arithmetic",
                description="A tool whose handler misbehaves.",
                input_model=CheckArithmeticInput,
                output_model=CheckArithmeticOutput,
                handler=handler,
                roles=frozenset({VALIDATION}),
            ),
        )
    )


def test_s5_ac_02_output_that_does_not_validate_is_an_output_error() -> None:
    invalid: dict[str, Any] = {"ok": "maybe", "issues": 5}
    broken = _one_tool_registry(lambda _: CheckArithmeticOutput.model_construct(**invalid))

    with pytest.raises(ToolOutputError, match="check_arithmetic") as raised:
        broken.for_role(VALIDATION).call("check_arithmetic", _check_arithmetic_payload())

    assert not isinstance(raised.value, ToolError)  # a bug in the tool, not the caller's fault


def test_s5_ac_02_output_of_the_wrong_model_is_an_output_error() -> None:
    wrong = _one_tool_registry(lambda _: OpenReviewOutput(review_id=1))

    with pytest.raises(ToolOutputError, match="check_arithmetic"):
        wrong.for_role(VALIDATION).call("check_arithmetic", _check_arithmetic_payload())


# --- the registry itself ------------------------------------------------------------------------


def test_two_tools_with_one_name_are_refused(registry: ToolRegistry) -> None:
    (first, *_) = registry.specs

    with pytest.raises(ValueError, match=first.name):
        ToolRegistry((first, first))


def test_a_tool_no_role_may_call_is_refused() -> None:
    with pytest.raises(ValueError, match="check_arithmetic"):
        ToolRegistry(
            (
                ToolSpec(
                    name="check_arithmetic",
                    description="Nobody may call this.",
                    input_model=CheckArithmeticInput,
                    output_model=CheckArithmeticOutput,
                    handler=lambda _: CheckArithmeticOutput(ok=True, issues=[]),
                    roles=frozenset(),
                ),
            )
        )
