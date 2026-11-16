"""The slice A tools through the in-process gateway (agent-core plan 2.3).

Pinned (docs/tests/agent-core-A.md):
- ``check_arithmetic {"receipt": <ExtractionOutput fields without confidence>}`` ->
  ``{"ok", "issues": [{"code", "message", "expected", "actual", "position"}]}`` with the domain
  issues verbatim and Money as ``{"amount_cents", "currency"}``.
- ``save_receipt`` (discounts-and-paid-prices plan, TC-87): ``discounts_linked`` counts every
  effective link (kept and new, A4) and ``discounts_receipt_level`` lists
  ``{"position", "raw_text", "reason"}`` in receipt order.
"""

import pytest

from paper_trail.application.payloads import DraftPayload
from paper_trail.domain.enums import AgentRole, ProcessingStage, ReceiptStatus
from paper_trail.domain.models import JsonValue, ReceiptDraft
from paper_trail.domain.rules.discounts import Refusal
from paper_trail.tools.inprocess import InProcessToolGateway
from tests.agent_harness import Harness, build_harness
from tests.extractions import (
    LINE3_ISSUE,
    SUBTOTAL_ISSUE,
    extraction,
    inconsistent_extraction,
    without_confidence,
)
from tests.factories import PRINTED_LINKS, PRINTED_MULTIPLE, printed_draft, receipt
from tests.fakes.reader import ScriptedReader
from tests.storage import in_unit
from tests.support import require_id


@pytest.fixture
def harness() -> Harness:
    return build_harness(ScriptedReader())


@pytest.fixture
def gateway(harness: Harness) -> InProcessToolGateway:
    return InProcessToolGateway(harness.registry)


def test_check_arithmetic_returns_the_domain_issues_verbatim(
    gateway: InProcessToolGateway,
) -> None:
    receipt = without_confidence(inconsistent_extraction())

    result = gateway.call(AgentRole.VALIDATION, "check_arithmetic", {"receipt": receipt})

    assert result["ok"] is False
    issues = result["issues"]
    assert isinstance(issues, list)
    assert [issue["message"] for issue in issues if isinstance(issue, dict)] == [
        LINE3_ISSUE,
        SUBTOTAL_ISSUE,
    ]
    assert issues[0] == {
        "code": "line_math",
        "message": LINE3_ISSUE,
        "expected": {"amount_cents": 998, "currency": "CAD"},
        "actual": {"amount_cents": 989, "currency": "CAD"},
        "position": 2,
    }


def test_check_arithmetic_of_a_consistent_receipt_is_ok(gateway: InProcessToolGateway) -> None:
    result = gateway.call(
        AgentRole.VALIDATION, "check_arithmetic", {"receipt": without_confidence(extraction())}
    )

    assert result == {"ok": True, "issues": []}


def _save(gateway: InProcessToolGateway, receipt_id: int, draft: ReceiptDraft) -> dict[str, object]:
    payload: JsonValue = DraftPayload.from_draft(draft).model_dump(mode="json")
    return dict(
        gateway.call(
            AgentRole.SUPERVISOR, "save_receipt", {"receipt_id": receipt_id, "draft": payload}
        )
    )


def test_tc_87_save_receipt_reports_the_effective_discount_links(
    harness: Harness, gateway: InProcessToolGateway
) -> None:
    """PRINTED: seven links and the MULTIPLE at receipt level; saving the draft again with its
    links already on the lines reports the same counts (effective, not new)."""
    stored = receipt(status=ReceiptStatus.PROCESSING, stage=ProcessingStage.READING)
    receipt_id = require_id(in_unit(harness.uow_factory, lambda uow: uow.receipts.add(stored)).id)
    expected = {
        "receipt_id": receipt_id,
        "status": "processing",
        "stage": "matching",
        "item_count": 22,
        "masked_pans": 0,
        "discounts_linked": 7,
        "discounts_receipt_level": [
            {"position": PRINTED_MULTIPLE, "raw_text": "MULTIPLE", "reason": Refusal.MULTIPLE}
        ],
    }

    first = _save(gateway, receipt_id, printed_draft())
    again = _save(gateway, receipt_id, printed_draft(PRINTED_LINKS))

    assert (first, again) == (expected, expected)
