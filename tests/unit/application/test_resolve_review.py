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
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from paper_trail.application.receipts import GetPendingReview, InconsistentDraftError
from paper_trail.config import Settings
from paper_trail.domain.enums import ProcessingStage, ReceiptStatus, ReviewStatus
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import ReceiptDraft
from paper_trail.domain.money import Money
from paper_trail.domain.rules.arithmetic import IssueCode, check_arithmetic
from tests.agent_harness import (
    Harness,
    agent_settings,
    build_harness,
)
from tests.extractions import PURCHASED_ON, answer, codex_answer, inconsistent_answer
from tests.factories import MASKED_TEST_PAN, TEST_PAN, cad, draft_with, line_item, receipt_draft
from tests.fakes.reader import ScriptedReader
from tests.fakes.tools import CrashAfterToolGateway
from tests.pans import card_numbers
from tests.support import require_id


def _parked(reader: ScriptedReader, settings: Settings | None = None) -> tuple[Harness, int, int]:
    """A receipt parked for review; returns (harness, receipt_id, review_id)."""
    harness = build_harness(reader, settings=settings)
    graph = harness.process_image().graph
    assert graph is not None
    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    return harness, graph.receipt_id, require_id(graph.review_id)


def _validation_failed() -> tuple[Harness, int, int]:
    return _parked(ScriptedReader(inconsistent_answer()), agent_settings(max_attempts=1))


def _corrected() -> ReceiptDraft:
    """What the person typed in: the receipt as it really is (consistent)."""
    return receipt_draft(store_name="Costco", ocr_text=None, image_path=None)


def test_s3_ac_07_a_consistent_draft_completes_the_same_thread() -> None:
    """S3-AC-07 / AC-A-08: resumes from the checkpoint (no new extraction), persists the
    person's draft, COMPLETED, review RESOLVED."""
    harness, receipt_id, review_id = _validation_failed()
    reader = harness.reader
    assert isinstance(reader, ScriptedReader)
    calls_before = reader.calls

    receipt = harness.resolve(review_id, _corrected(), force=True, reason="checked the paper")

    assert reader.calls == calls_before
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
    assert stored.ocr_text is None  # claude-extraction R6: no longer written
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


def test_ac_a_08_accepting_an_inconsistent_extraction_needs_force() -> None:
    harness, _, review_id = _validation_failed()

    with pytest.raises(InconsistentDraftError):
        harness.resolve(review_id)

    receipt = harness.resolve(review_id, force=True, reason="checked against the paper")
    assert receipt.status is ReceiptStatus.COMPLETED


def test_ac_a_08_nothing_to_accept_after_the_answers_failed_the_schema() -> None:
    """CE-TC-024: an answer that fails the schema is a failed attempt; after two there is no
    reading to accept, only a person's file."""
    harness, _, review_id = _parked(ScriptedReader({"items": []}, {"items": []}))

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
    assert pending.ocr_text is None


def test_unknown_and_closed_reviews_are_refused() -> None:
    harness, _, review_id = _validation_failed()

    with pytest.raises(NotFoundError):
        harness.resolve(review_id + 100, _corrected(), force=True, reason="checked the paper")
    harness.resolve(review_id, _corrected(), force=True, reason="checked the paper")
    with pytest.raises(ConflictError):
        harness.resolve(review_id, _corrected(), force=True, reason="checked the paper")


def test_the_person_s_draft_is_masked_like_any_other() -> None:
    """ADR-0011: a card number typed into the correction is masked before it is stored."""
    harness, receipt_id, review_id = _validation_failed()
    draft = draft_with([line_item(raw_text=f"EGGS {TEST_PAN}")], subtotal=None, tax=None, total=899)

    harness.resolve(review_id, draft, force=True, reason="checked the paper")

    stored = harness.receipt(receipt_id)
    assert stored.items[0].raw_text == f"EGGS {MASKED_TEST_PAN}"
    assert card_numbers(repr(stored)) == []


# --- review gaps A1: accepting as read is judged on the reading's own issues -------------------


def _codex() -> tuple[Harness, int, int]:
    """F-CODEX parked: 2 x 5.00 printed for a line of 8.00 (LINE_MATH only)."""
    return _parked(ScriptedReader(codex_answer()), agent_settings(max_attempts=1))


@pytest.mark.parametrize(
    ("force", "reason", "error"),
    [
        (False, None, InconsistentDraftError),
        (True, None, DomainValidationError),
        (True, "", DomainValidationError),
        (True, "   ", DomainValidationError),
    ],
    ids=["no-force", "force-no-reason", "force-empty-reason", "force-blank-reason"],
)
def test_a1_accepting_a_reading_with_issues_needs_force_and_a_reason(
    force: bool, reason: str | None, error: type[Exception]
) -> None:
    harness, receipt_id, review_id = _codex()
    reader = harness.reader
    assert isinstance(reader, ScriptedReader)

    with pytest.raises(error) as raised:
        harness.resolve(review_id, force=force, reason=reason)

    if isinstance(raised.value, InconsistentDraftError):
        issues = raised.value.report.issues
        assert [(issue.code, issue.position) for issue in issues] == [(IssueCode.LINE_MATH, 0)]
        assert "2 each x 5.00 = 10.00" in issues[0].message
    else:
        assert "reason" in str(raised.value)
    assert harness.review(review_id).status is ReviewStatus.OPEN
    assert harness.receipt(receipt_id).status is ReceiptStatus.NEEDS_REVIEW
    assert harness.runner.waiting_for_review(receipt_id)
    assert reader.calls == 1


def test_a1_a_forced_accept_saves_the_reading_as_printed_and_remembers_why() -> None:
    harness, receipt_id, review_id = _codex()

    receipt = harness.resolve(review_id, force=True, reason="printed price is per pack")

    assert (receipt.status, receipt.total) == (ReceiptStatus.COMPLETED, cad(800))
    assert [(line.raw_text, line.unit_price, line.line_total) for line in receipt.items] == [
        ("KS SOURDOUGH", cad(500), cad(800))
    ]
    assert harness.review(review_id).status is ReviewStatus.RESOLVED
    correction = harness.latest_correction(f"receipt:{receipt_id}.arithmetic")
    assert correction is not None
    assert correction.new_value == "printed price is per pack"


def _first_line(draft: ReceiptDraft, **changes: Any) -> ReceiptDraft:
    first, *rest = draft.items
    return replace(draft, items=(replace(first, **changes), *rest))


def test_a3_the_review_s_draft_keeps_the_printed_unit_price_and_needs_a_reason() -> None:
    """The review's draft carries the printed 5.00 (not a rebuilt 4.00): refused without a
    reason with the reading's LINE_MATH, and stored as printed when forced."""
    harness, receipt_id, review_id = _codex()
    pending = harness.runner.pending_draft(receipt_id)
    assert pending is not None
    assert pending.items[0].unit_price == cad(500)

    with pytest.raises(InconsistentDraftError) as raised:
        harness.resolve(review_id, pending)
    receipt = harness.resolve(review_id, pending, force=True, reason="per pack")

    assert [issue.code for issue in raised.value.report.issues] == [IssueCode.LINE_MATH]
    assert (receipt.status, receipt.items[0].unit_price) == (ReceiptStatus.COMPLETED, cad(500))


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"purchased_on": None}, IssueCode.MISSING_DATE),
        ({"store_name": "  "}, IssueCode.MISSING_STORE),
    ],
    ids=["no-date", "blank-store"],
)
def test_r2_a_person_s_draft_needs_its_store_and_date(
    change: dict[str, Any], code: IssueCode
) -> None:
    harness, receipt_id, review_id = _validation_failed()
    draft = replace(_corrected(), **change)

    with pytest.raises(InconsistentDraftError) as raised:
        harness.resolve(review_id, draft)
    receipt = harness.resolve(review_id, draft, force=True, reason="not printed")

    assert [issue.code for issue in raised.value.report.issues] == [code]
    assert receipt.status is ReceiptStatus.COMPLETED
    assert harness.latest_correction(f"receipt:{receipt_id}.arithmetic") is not None


def test_a1_a_reading_missing_only_its_date_needs_force_to_accept() -> None:
    """F-NODATE: accepted as read it needs force + reason and is saved undated."""
    harness, receipt_id, review_id = _parked(
        ScriptedReader(answer(purchased_on=None)), agent_settings(max_attempts=1)
    )
    pending = harness.runner.pending_draft(receipt_id)
    assert pending is not None
    assert check_arithmetic(pending).ok

    with pytest.raises(InconsistentDraftError) as raised:
        harness.resolve(review_id)
    forced = harness.resolve(review_id, force=True, reason="no date printed")

    assert [issue.code for issue in raised.value.report.issues] == [IssueCode.MISSING_DATE]
    assert (forced.status, forced.purchased_on) == (ReceiptStatus.COMPLETED, None)
    correction = harness.latest_correction(f"receipt:{receipt_id}.arithmetic")
    assert correction is not None
    assert correction.new_value == "no date printed"


# --- review gaps A3: a crash between opening the review and the supervisor's checkpoint ------


def _gap() -> tuple[Harness, int, int]:
    """F-GAP: the supervisor opened the review, then the run died before its checkpoint."""
    saver = InMemorySaver()
    settings = agent_settings(max_attempts=1)
    first = build_harness(
        ScriptedReader(inconsistent_answer()),
        settings=settings,
        checkpointer=saver,
        wrap_tools=lambda gateway: CrashAfterToolGateway(gateway, "open_review"),
    )
    crashed = first.process_image()
    assert crashed.failure is not None
    (opened,) = first.open_reviews()
    later = build_harness(
        ScriptedReader(),
        settings=settings,
        checkpointer=saver,
        uow_factory=first.uow_factory,
        images=first.images,
    )
    assert not later.runner.waiting_for_review(crashed.receipt_id)
    return later, crashed.receipt_id, require_id(opened.id)


@pytest.mark.parametrize("accept", [False, True], ids=["corrected", "forced-accept"])
def test_c04_an_answer_given_in_the_gap_is_consumed_never_dropped(accept: bool) -> None:
    """``review resolve`` (C-09's call) continues the run to the review it opened, then gives
    the answer: the receipt holds the answer's values and only then is the review closed."""
    harness, receipt_id, review_id = _gap()

    if accept:
        receipt = harness.resolve(review_id, force=True, reason="checked the paper")
    else:
        receipt = harness.resolve(review_id, _corrected(), force=True, reason="checked the paper")

    stored = harness.receipt(receipt_id)
    assert receipt.status is ReceiptStatus.COMPLETED
    assert (stored.status, stored.total) == (
        ReceiptStatus.COMPLETED,
        cad(2683) if accept else cad(1635),
    )
    expected = ["KS ORG EGGS 24CT", "KS 2% MILK 4L"]
    assert [line.raw_text for line in stored.items] == (
        [*expected, "KS SOURDOUGH"] if accept else expected
    )
    assert harness.review(review_id).status is ReviewStatus.RESOLVED
    assert harness.open_reviews() == ()
    assert harness.runner.answer_taken(receipt_id)
    assert isinstance(harness.reader, ScriptedReader)
    assert harness.reader.calls == 0


# --- review gaps A4: reviews ``receipts import`` opened have no thread ------------------------


def _imported() -> tuple[Harness, int, int]:
    """F-IMPORT: an imported draft whose milk line is 6.85 (it does not add up)."""
    harness = build_harness(ScriptedReader())
    eggs, milk = receipt_draft().items
    imported = harness.record(receipt_draft(items=(eggs, replace(milk, line_total=cad(685)))))
    assert imported.review is not None
    assert imported.review.thread_id is None
    return harness, require_id(imported.receipt.id), require_id(imported.review.id)


@pytest.mark.parametrize(
    ("accept", "force", "reason", "error"),
    [
        (False, False, None, InconsistentDraftError),
        (True, False, None, InconsistentDraftError),
        (False, True, "  ", DomainValidationError),
        (True, True, None, DomainValidationError),
    ],
    ids=["draft-no-force", "accept-no-force", "draft-blank-reason", "accept-no-reason"],
)
def test_i05_an_imported_answer_that_does_not_add_up_needs_force_and_a_reason(
    accept: bool, force: bool, reason: str | None, error: type[Exception]
) -> None:
    harness, receipt_id, review_id = _imported()
    eggs, milk = receipt_draft().items
    wrong = receipt_draft(items=(eggs, replace(milk, line_total=cad(660))))

    with pytest.raises(error) as raised:
        harness.resolve(review_id, None if accept else wrong, force=force, reason=reason)

    if isinstance(raised.value, InconsistentDraftError):
        assert {issue.code for issue in raised.value.report.issues} == {
            IssueCode.LINE_MATH,
            IssueCode.ITEMS_VS_SUBTOTAL,
        }
    assert harness.review(review_id).status is ReviewStatus.OPEN
    assert harness.receipt(receipt_id).items[1].line_total == cad(685)


@pytest.mark.parametrize("accept", [False, True], ids=["forced-draft", "forced-accept"])
def test_i05_an_imported_answer_forced_with_a_reason_is_saved_and_remembered(
    accept: bool,
) -> None:
    harness, receipt_id, review_id = _imported()
    eggs, milk = receipt_draft().items
    wrong = receipt_draft(items=(eggs, replace(milk, line_total=cad(660))))

    receipt = harness.resolve(
        review_id, None if accept else wrong, force=True, reason=f"paid with {TEST_PAN}"
    )

    assert receipt.status is ReceiptStatus.COMPLETED
    assert harness.receipt(receipt_id).items[1].line_total == cad(685 if accept else 660)
    assert harness.review(review_id).status is ReviewStatus.RESOLVED
    correction = harness.latest_correction(f"receipt:{receipt_id}.arithmetic")
    assert correction is not None
    assert correction.new_value == f"paid with {MASKED_TEST_PAN}"


def test_i05_an_imported_receipt_in_two_currencies_needs_force_saved_as_shown_or_accepted() -> None:
    """The import stored the USD milk as CAD cents, so its stored contents add up; saving them
    unchanged, or accepting them, is still judged on the CURRENCY_MISMATCH it was opened
    with."""
    harness = build_harness(ScriptedReader())
    eggs, milk = receipt_draft().items
    usd = replace(milk, unit_price=Money(329, "USD"), line_total=Money(658, "USD"))
    imported = harness.record(receipt_draft(items=(eggs, usd)))
    assert imported.review is not None
    review_id = require_id(imported.review.id)
    pending = GetPendingReview(harness.uow_factory, harness.runner)(review_id).draft
    assert pending is not None

    for given in (None, pending):
        with pytest.raises(InconsistentDraftError) as raised:
            harness.resolve(review_id, given)
        assert {issue.code for issue in raised.value.report.issues} == {IssueCode.CURRENCY_MISMATCH}
    assert harness.review(review_id).status is ReviewStatus.OPEN
