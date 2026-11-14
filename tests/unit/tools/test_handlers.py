"""The slice A tools through the in-process gateway (AC-A-04, agent-core plan 2.3).

Pinned (docs/tests/agent-core-A.md):
- ``ocr_read {"image_path"}`` -> ``{"text", "mean_confidence", "rotation_degrees",
  "masked_pans"}`` (``rotation_degrees``: docs/tests/ocr-rotation.md): the stored
  image is read through ``ImageStore.open``, OCR'd, then ``mask_pans`` runs before the text
  leaves the tool; an unknown image path is a ``NotFoundError``.
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
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import JsonValue, ReceiptDraft
from paper_trail.domain.rules.discounts import Refusal
from paper_trail.tools.inprocess import InProcessToolGateway
from tests.agent_harness import Harness, build_harness
from tests.extractions import (
    COSTCO_OCR,
    LINE3_ISSUE,
    MASKED_PAN,
    SUBTOTAL_ISSUE,
    extraction,
    inconsistent_extraction,
    without_confidence,
)
from tests.factories import PRINTED_LINKS, PRINTED_MULTIPLE, printed_draft, receipt
from tests.fakes.images import fake_png
from tests.fakes.llm import ScriptedLlm
from tests.pans import card_numbers
from tests.storage import in_unit
from tests.support import require_id


@pytest.fixture
def harness() -> Harness:
    return build_harness(ScriptedLlm())


@pytest.fixture
def gateway(harness: Harness) -> InProcessToolGateway:
    return InProcessToolGateway(harness.registry)


def test_ac_a_04_ocr_read_masks_card_numbers(
    harness: Harness, gateway: InProcessToolGateway
) -> None:
    """AC-A-04: the fake OCR returns VISA 4111 1111 1111 1111; the tool returns it masked."""
    stored = harness.images.save(fake_png("costco"))

    result = gateway.call(AgentRole.EXTRACTION, "ocr_read", {"image_path": stored.relative_path})

    assert harness.ocr.images == [fake_png("costco")]
    assert result["masked_pans"] == 1
    assert result["mean_confidence"] == pytest.approx(0.87)
    text = result["text"]
    assert isinstance(text, str)
    assert text == COSTCO_OCR.replace("4111 1111 1111 1111", MASKED_PAN)
    assert card_numbers(text) == []


def test_ocr_read_returns_the_rotation_the_ocr_chose(
    harness: Harness, gateway: InProcessToolGateway
) -> None:
    """docs/tests/ocr-rotation.md: rotation_degrees is passed through like mean_confidence."""
    harness.ocr.rotation_degrees = 90
    stored = harness.images.save(fake_png("sideways"))

    result = gateway.call(AgentRole.EXTRACTION, "ocr_read", {"image_path": stored.relative_path})

    assert result["rotation_degrees"] == 90
    assert result["mean_confidence"] == pytest.approx(0.87)


def test_ocr_read_of_an_upright_photo_reports_no_rotation(
    harness: Harness, gateway: InProcessToolGateway
) -> None:
    stored = harness.images.save(fake_png("costco"))

    result = gateway.call(AgentRole.EXTRACTION, "ocr_read", {"image_path": stored.relative_path})

    assert result["rotation_degrees"] == 0


def test_ocr_read_of_an_unknown_image_is_not_found(gateway: InProcessToolGateway) -> None:
    with pytest.raises(NotFoundError):
        gateway.call(AgentRole.EXTRACTION, "ocr_read", {"image_path": "no/such/image.jpg"})


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
