"""ToolGateway contract (PLAN 7, AC-A-05, S5-AC-04): what every gateway does, on any transport.

Runs for the in-process gateway and for ``McpToolGateway`` against the production MCP app served
on a loopback port (``tests/mcp_server.py``), each over the fake and the SQLite unit of work.

Pinned (docs/tests/agent-core-B.md):
- ``ToolGateway.describe(role) -> tuple[ToolDescription, ...]``: the role's tools sorted by name,
  ``ToolDescription(name, description)`` from the registry's specs; identical over MCP.
- Errors keep their class over MCP: ``ToolNotPermittedError`` (with ``.role``, ``.tool`` and the
  message ``denied: role=<role> tool=<tool>``), ``UnknownToolError``, ``ToolInputError`` (naming
  the field), and the domain errors ``NotFoundError`` and ``ConflictError`` raised by a tool.
- Results are plain JSON and equal over both transports.
- Slice A review note 4: ``open_review`` is idempotent per receipt and thread. A second call for
  a receipt that already has an OPEN review with the same ``thread_id`` returns that review's
  id without opening another, and the receipt is (again) NEEDS_REVIEW/NEEDS_REVIEW; an OPEN
  review with another thread (e.g. opened by a JSON import) is still a ``ConflictError``.
"""

import json
from collections.abc import Iterator

import pytest

from paper_trail.domain.enums import AgentRole, ProcessingStage, ReceiptStatus
from paper_trail.domain.errors import ConflictError, NotFoundError
from paper_trail.domain.models import JsonValue
from paper_trail.ports.tools import (
    ToolDescription,
    ToolGateway,
    ToolInputError,
    ToolNotPermittedError,
    UnknownToolError,
)
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from paper_trail.tools.inprocess import InProcessToolGateway
from tests.agent_harness import TOOL_TRANSPORTS, Harness, build_harness, mcp_gateway
from tests.extractions import extraction, inconsistent_extraction, without_confidence
from tests.factories import receipt
from tests.fakes.images import fake_png
from tests.fakes.llm import ScriptedLlm
from tests.mcp_server import serve_mcp
from tests.storage import in_unit
from tests.support import require_id


@pytest.fixture
def harness(uow_factory: UnitOfWorkFactory) -> Harness:
    return build_harness(ScriptedLlm(), uow_factory=uow_factory)


@pytest.fixture(params=TOOL_TRANSPORTS)
def gateway(request: pytest.FixtureRequest, harness: Harness) -> Iterator[ToolGateway]:
    if request.param == "inprocess":
        yield InProcessToolGateway(harness.registry)
        return
    with serve_mcp(harness.registry) as url:
        yield mcp_gateway(url)


def _processing_receipt(harness: Harness) -> int:
    stored = receipt(status=ReceiptStatus.PROCESSING, stage=ProcessingStage.READING)
    return require_id(in_unit(harness.uow_factory, lambda uow: uow.receipts.add(stored)).id)


def _open_review(gateway: ToolGateway, receipt_id: int, thread: str) -> int:
    result = gateway.call(
        AgentRole.SUPERVISOR,
        "open_review",
        {
            "receipt_id": receipt_id,
            "reason": "validation_failed",
            "details": {"issues": []},
            "thread_id": thread,
        },
    )
    review_id = result["review_id"]
    assert isinstance(review_id, int)
    return review_id


def test_an_allowed_call_returns_json(harness: Harness, gateway: ToolGateway) -> None:
    path = harness.images.save(fake_png("receipt")).relative_path

    result = gateway.call(AgentRole.EXTRACTION, "ocr_read", {"image_path": path})

    assert json.loads(json.dumps(result)) == dict(result)
    assert set(result) == {"text", "mean_confidence", "rotation_degrees", "masked_pans"}


def test_ocr_read_reports_the_rotation_over_every_transport(
    harness: Harness, gateway: ToolGateway
) -> None:
    """docs/tests/ocr-rotation.md: the chosen rotation travels with the text (MCP included)."""
    harness.ocr.rotation_degrees = 270
    path = harness.images.save(fake_png("sideways")).relative_path

    result = gateway.call(AgentRole.EXTRACTION, "ocr_read", {"image_path": path})

    assert result["rotation_degrees"] == 270


def test_results_are_the_same_over_every_transport(harness: Harness, gateway: ToolGateway) -> None:
    payload: dict[str, JsonValue] = {"receipt": without_confidence(inconsistent_extraction())}
    direct = InProcessToolGateway(harness.registry)

    assert dict(gateway.call(AgentRole.VALIDATION, "check_arithmetic", payload)) == dict(
        direct.call(AgentRole.VALIDATION, "check_arithmetic", payload)
    )


def test_a_call_outside_the_role_is_denied(gateway: ToolGateway) -> None:
    payload: dict[str, JsonValue] = {"receipt": without_confidence(extraction())}

    with pytest.raises(ToolNotPermittedError) as raised:
        gateway.call(AgentRole.EXTRACTION, "check_arithmetic", payload)

    assert str(raised.value) == "denied: role=extraction tool=check_arithmetic"
    assert (raised.value.role, raised.value.tool) == (AgentRole.EXTRACTION, "check_arithmetic")


def test_a_write_tool_is_denied_to_a_specialist(gateway: ToolGateway) -> None:
    """E3: a matching agent may not save a receipt, whatever it sends."""
    with pytest.raises(ToolNotPermittedError, match="denied: role=matching tool=save_receipt"):
        gateway.call(AgentRole.MATCHING, "save_receipt", {"receipt_id": 1, "draft": {}})


def test_an_unknown_tool_is_reported(gateway: ToolGateway) -> None:
    with pytest.raises(UnknownToolError, match="drop_tables"):
        gateway.call(AgentRole.SUPERVISOR, "drop_tables", {})


def test_invalid_input_is_reported_with_the_field(gateway: ToolGateway) -> None:
    with pytest.raises(ToolInputError, match="receipt"):
        gateway.call(AgentRole.VALIDATION, "check_arithmetic", {"receipt": "everything adds up"})


def test_a_domain_error_of_the_tool_keeps_its_class(gateway: ToolGateway) -> None:
    with pytest.raises(NotFoundError):
        gateway.call(AgentRole.EXTRACTION, "ocr_read", {"image_path": "no/such/image.jpg"})


@pytest.mark.parametrize("role", list(AgentRole))
def test_describe_lists_the_roles_tools_from_the_registry(
    harness: Harness, gateway: ToolGateway, role: AgentRole
) -> None:
    """AC-B-04: the same tools, with their descriptions, over every transport."""
    expected = tuple(
        ToolDescription(spec.name, spec.description)
        for spec in sorted(harness.registry.specs, key=lambda spec: spec.name)
        if role in spec.roles
    )

    assert gateway.describe(role) == expected
    assert tuple(tool.name for tool in expected) == harness.registry.for_role(role).names


# --- slice A review note 4: open_review is idempotent per receipt and thread -------------------


def test_opening_the_same_review_twice_returns_the_first(
    harness: Harness, gateway: ToolGateway
) -> None:
    receipt_id = _processing_receipt(harness)
    thread = f"receipt-{receipt_id}"

    first = _open_review(gateway, receipt_id, thread)
    again = _open_review(gateway, receipt_id, thread)

    assert again == first
    assert [review.id for review in harness.open_reviews()] == [first]
    stored = harness.receipt(receipt_id)
    assert (stored.status, stored.stage) == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
    )


def test_an_open_review_of_another_thread_is_still_a_conflict(
    harness: Harness, gateway: ToolGateway
) -> None:
    receipt_id = _processing_receipt(harness)
    first = _open_review(gateway, receipt_id, "import")

    with pytest.raises(ConflictError):
        _open_review(gateway, receipt_id, f"receipt-{receipt_id}")

    assert [review.id for review in harness.open_reviews()] == [first]


def test_a_closed_review_does_not_count(harness: Harness, gateway: ToolGateway) -> None:
    receipt_id = _processing_receipt(harness)
    thread = f"receipt-{receipt_id}"
    first = _open_review(gateway, receipt_id, thread)
    in_unit(harness.uow_factory, lambda uow: uow.reviews.resolve(first))

    second = _open_review(gateway, receipt_id, thread)

    assert second != first
    assert [review.id for review in harness.open_reviews()] == [second]
