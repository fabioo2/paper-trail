"""``ResolveReview``: a person's answer resumes the parked graph thread (S3-AC-07, AC-A-08).

Pinned (docs/tests/agent-core-A.md):
- ``ResolveReview(uow_factory, runner, record_receipt, clock)`` in
  ``paper_trail.application.receipts``; ``__call__(review_id, draft=None, *, force=False,
  reason=None) -> Receipt`` (the stored receipt after the graph finished).
- ``draft=None`` accepts the extraction the graph is holding (``runner.pending_draft``);
  ``DomainValidationError`` when there is none (an EXTRACTION_FAILED review).
- The draft is checked with ``RecordReceipt.assess``; when it does not add up and ``force`` is
  False, ``InconsistentDraftError`` (a ``DomainValidationError`` carrying ``.report``) is raised
  and nothing changes. ``force`` needs a non-blank ``reason`` (``DomainValidationError``).
- A resumed draft is human-approved: the graph saves it COMPLETED even if forced through; a
  forced resolution records ``Correction(kind=FIELD, subject="receipt:<id>.arithmetic",
  old_value=None, new_value=reason, store_key=None, receipt_id=<id>)``.
- The review becomes RESOLVED only after the graph finished. Unknown review: NotFoundError;
  a review that is not OPEN: ConflictError.

In-process tools; the MCP transport is compared in
``tests/unit/agents/test_transport_equivalence.py`` (S5-AC-04).
"""

from dataclasses import replace

import pytest

from paper_trail.application.receipts import InconsistentDraftError
from paper_trail.config import Settings
from paper_trail.domain.enums import ProcessingStage, ReceiptStatus, ReviewStatus
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import ReceiptDraft
from paper_trail.domain.money import Money
from paper_trail.domain.rules.arithmetic import IssueCode
from tests.agent_harness import (
    Harness,
    agent_settings,
    build_harness,
)
from tests.extractions import PURCHASED_ON, extraction, inconsistent_extraction
from tests.factories import MASKED_TEST_PAN, TEST_PAN, cad, draft_with, line_item, receipt_draft
from tests.fakes.llm import ScriptedLlm
from tests.pans import card_numbers
from tests.support import require_id


def _parked(llm: ScriptedLlm, settings: Settings | None = None) -> tuple[Harness, int, int]:
    """A receipt parked for review; returns (harness, receipt_id, review_id)."""
    harness = build_harness(llm, settings=settings)
    graph = harness.process_image().graph
    assert graph is not None
    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    return harness, graph.receipt_id, require_id(graph.review_id)


def _validation_failed() -> tuple[Harness, int, int]:
    return _parked(ScriptedLlm(inconsistent_extraction()), agent_settings(max_text_attempts=1))


def _corrected() -> ReceiptDraft:
    """What the person typed in: the receipt as it really is (consistent)."""
    return receipt_draft(store_name="Costco", ocr_text=None, image_path=None)


def test_s3_ac_07_a_consistent_draft_completes_the_same_thread() -> None:
    """S3-AC-07 / AC-A-08: resumes from the checkpoint (no new extraction), persists the
    person's draft, COMPLETED, review RESOLVED."""
    harness, receipt_id, review_id = _validation_failed()
    calls_before = harness.llm.calls

    receipt = harness.resolve(review_id, _corrected())

    assert harness.llm.calls == calls_before
    assert receipt.id == receipt_id
    assert (receipt.status, receipt.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert receipt.store_name == "Costco"
    assert receipt.total == cad(1635)
    assert [item.raw_text for item in receipt.items] == ["KS ORG EGGS 24CT", "KS 2% MILK 4L"]
    review = harness.review(review_id)
    assert review.status is ReviewStatus.RESOLVED
    assert review.resolved_at is not None
    stored = harness.receipt(receipt_id)
    assert stored.image_sha256 is not None  # kept from the stored receipt
    assert stored.ocr_text is not None  # the draft has none: the graph's OCR text is saved
    resumed = [name for _, name in harness.steps(receipt_id)]
    assert resumed[-1] == "save_matches"  # slice C: matching follows the save
    assert "save_receipt" in resumed


def test_s3_ac_07_an_inconsistent_draft_is_refused_with_its_issues() -> None:
    """S3-AC-07: refused (the API's 422; the CLI's exit 2) with the arithmetic issues."""
    harness, receipt_id, review_id = _validation_failed()
    eggs, milk = receipt_draft().items
    wrong = receipt_draft(items=(eggs, replace(milk, line_total=cad(685))))

    with pytest.raises(InconsistentDraftError) as raised:
        harness.resolve(review_id, wrong)

    assert isinstance(raised.value, DomainValidationError)
    assert IssueCode.LINE_MATH in {issue.code for issue in raised.value.report.issues}
    assert harness.review(review_id).status is ReviewStatus.OPEN
    assert harness.receipt(receipt_id).status is ReceiptStatus.NEEDS_REVIEW


def test_s3_ac_07_force_with_a_reason_saves_an_inconsistent_draft() -> None:
    harness, receipt_id, review_id = _validation_failed()
    eggs, milk = receipt_draft().items
    wrong = receipt_draft(items=(eggs, replace(milk, line_total=cad(685))))

    receipt = harness.resolve(review_id, wrong, force=True, reason="total torn off")

    assert receipt.status is ReceiptStatus.COMPLETED
    assert receipt.items[1].line_total == cad(685)
    assert harness.review(review_id).status is ReviewStatus.RESOLVED
    correction = harness.latest_correction(f"receipt:{receipt_id}.arithmetic")
    assert correction is not None
    assert (correction.new_value, correction.receipt_id) == ("total torn off", receipt_id)


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_force_needs_a_reason(reason: str | None) -> None:
    harness, _, review_id = _validation_failed()
    eggs, milk = receipt_draft().items
    wrong = receipt_draft(items=(eggs, replace(milk, line_total=cad(685))))

    with pytest.raises(DomainValidationError, match="reason"):
        harness.resolve(review_id, wrong, force=True, reason=reason)

    assert harness.review(review_id).status is ReviewStatus.OPEN


def test_ac_a_08_accept_resumes_with_the_stored_extraction() -> None:
    """AC-A-08 (--accept): a LOW_CONFIDENCE extraction adds up, so it can be accepted as is."""
    harness, _, review_id = _parked(ScriptedLlm(extraction(confidence=0.2)))

    receipt = harness.resolve(review_id)

    assert harness.llm.calls == 1
    assert receipt.status is ReceiptStatus.COMPLETED
    assert receipt.purchased_on == PURCHASED_ON
    assert receipt.total == Money(2683, "CAD")
    assert harness.review(review_id).status is ReviewStatus.RESOLVED


def test_ac_a_08_accepting_an_inconsistent_extraction_needs_force() -> None:
    harness, _, review_id = _validation_failed()

    with pytest.raises(InconsistentDraftError):
        harness.resolve(review_id)

    receipt = harness.resolve(review_id, force=True, reason="checked against the paper")
    assert receipt.status is ReceiptStatus.COMPLETED


def test_ac_a_08_nothing_to_accept_after_extraction_failed() -> None:
    harness, _, review_id = _parked(ScriptedLlm("not JSON", "still not JSON"))

    assert harness.runner.pending_draft(harness.review(review_id).receipt_id) is None
    with pytest.raises(DomainValidationError, match=r"--file|draft"):
        harness.resolve(review_id)


def test_pending_draft_is_the_extraction_the_graph_holds() -> None:
    harness, receipt_id, _ = _validation_failed()

    pending = harness.runner.pending_draft(receipt_id)

    assert pending is not None
    assert pending.total == Money(2683, "CAD")
    assert pending.items[2].line_total == Money(989, "CAD")
    assert pending.purchased_on == PURCHASED_ON


def test_unknown_and_closed_reviews_are_refused() -> None:
    harness, _, review_id = _validation_failed()

    with pytest.raises(NotFoundError):
        harness.resolve(review_id + 100, _corrected())
    harness.resolve(review_id, _corrected())
    with pytest.raises(ConflictError):
        harness.resolve(review_id, _corrected())


def test_the_person_s_draft_is_masked_like_any_other() -> None:
    """ADR-0011: a card number typed into the correction is masked before it is stored."""
    harness, receipt_id, review_id = _validation_failed()
    draft = draft_with([line_item(raw_text=f"EGGS {TEST_PAN}")], subtotal=None, tax=None, total=899)

    harness.resolve(review_id, draft)

    stored = harness.receipt(receipt_id)
    assert stored.items[0].raw_text == f"EGGS {MASKED_TEST_PAN}"
    assert card_numbers(repr(stored)) == []


def test_a_review_without_an_agent_thread_cannot_be_resumed() -> None:
    """A Stage 1 import opens reviews with no graph thread: there is nothing to resume."""
    harness = build_harness(ScriptedLlm())
    eggs, milk = receipt_draft().items
    imported = harness.record(receipt_draft(items=(eggs, replace(milk, line_total=cad(685)))))
    assert imported.review is not None

    with pytest.raises(DomainValidationError, match="no run to resume"):
        harness.resolve(require_id(imported.review.id), _corrected())

    assert harness.review(require_id(imported.review.id)).status is ReviewStatus.OPEN
