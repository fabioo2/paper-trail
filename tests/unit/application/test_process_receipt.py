"""``ProcessReceiptFile``: one image file in, one receipt and one graph run out.

Covers AC-A-02 (image dedup through the use case, S2-AC-03 pulled forward) and the use case's
own rules (agent-core plan 2.2).

Pinned (docs/tests/agent-core-A.md):
- a new receipt is created PROCESSING/READING in one unit of work before the graph starts, with
  an empty store, currency CAD, no total, no items, ``image_path``/``image_sha256`` from the
  stored image and no ``ocr_text`` (claude-extraction plan R6: it is no longer written);
- the same image bytes again return the first receipt with ``duplicate=True`` and
  ``graph=None``: nothing is stored again, the graph does not run, Claude is not called;
  ``reprocess=True`` is slice C (``--reprocess``) and not covered here;
- a file that is not an image raises ``UnsupportedMediaError`` and creates no receipt.
"""

import pytest

from paper_trail.application.process_receipt import (
    ProcessingStoppedError,
    ProcessOutcome,
    ReceiptSource,
    failure_reason,
)
from paper_trail.domain.enums import FailureReason, ProcessingStage, ReceiptStatus
from paper_trail.domain.errors import ConflictError, UnsupportedMediaError
from paper_trail.domain.paging import PageRequest
from paper_trail.ports.reader import (
    BudgetReachedError,
    ReaderKeyError,
    ReaderOutputError,
    ReaderRefusedError,
    ReaderTruncatedError,
    ReaderUnavailableError,
)
from tests.agent_harness import build_harness
from tests.extractions import NOW, answer
from tests.fakes.reader import ScriptedReader


def test_ac_a_02_same_image_twice_returns_the_first_receipt_without_a_new_run() -> None:
    """AC-A-02: duplicate=True, the first receipt id, no second graph run."""
    reader = ScriptedReader(answer())
    harness = build_harness(reader)

    first = harness.process_image("costco")
    second = harness.process_image("costco")

    assert first.duplicate is False
    assert second == ProcessOutcome(receipt_id=first.receipt_id, duplicate=True, graph=None)
    assert reader.calls == 1
    assert len(harness.images.files) == 1
    with harness.uow_factory(read_only=True) as uow:
        assert len(uow.receipts.list(PageRequest(limit=10)).items) == 1


def test_ac_a_02_different_images_are_different_receipts() -> None:
    harness = build_harness(ScriptedReader(then=answer()))

    first = harness.process_image("costco")
    second = harness.process_image("freshco")

    assert first.receipt_id != second.receipt_id
    assert second.duplicate is False
    assert (
        harness.receipt(first.receipt_id).image_sha256
        != harness.receipt(second.receipt_id).image_sha256
    )


def test_the_receipt_exists_before_the_graph_runs() -> None:
    """A run that cannot reach the model still leaves a PROCESSING receipt to resume."""
    harness = build_harness(ScriptedReader(RuntimeError("model exploded")))

    outcome = harness.process_image("costco")

    receipt = harness.receipt(outcome.receipt_id)
    assert receipt.created_at == NOW
    stored = next(iter(harness.images.files))
    assert receipt.image_path == stored
    assert receipt.image_sha256 is not None
    assert stored.endswith(f"{receipt.image_sha256}.jpg")
    assert receipt.ocr_text is None


def test_an_unexpected_error_fails_the_receipt_and_is_reported() -> None:
    """AC-A-10 (exit 3): the receipt is FAILED with an ERROR trace event; the use case
    returns the failure instead of raising, so a batch continues with the next file."""
    harness = build_harness(ScriptedReader(RuntimeError("model exploded")))

    outcome = harness.process_image("costco")

    assert outcome.graph is None
    assert outcome.failure is not None
    assert "model exploded" in outcome.failure
    receipt = harness.receipt(outcome.receipt_id)
    assert (receipt.status, receipt.stage) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    assert harness.events(outcome.receipt_id)[-1].kind.value == "error"


def test_a_non_image_creates_no_receipt() -> None:
    harness = build_harness(ScriptedReader())

    with pytest.raises(UnsupportedMediaError):
        harness.process(ReceiptSource(name="notes.jpg", image=b"%PDF-1.7 not an image"))

    with harness.uow_factory(read_only=True) as uow:
        assert uow.receipts.list(PageRequest(limit=10)).items == ()


def _stopped(cause: BudgetReachedError) -> ProcessingStoppedError:
    """A stopped run as ``ProcessReceiptFile`` raises it: from its cause."""
    try:
        raise ProcessingStoppedError(1, cause) from cause
    except ProcessingStoppedError as stopped:
        return stopped


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (BudgetReachedError("monthly Claude budget reached"), FailureReason.BUDGET_REACHED),
        (ReaderKeyError("no Claude API key"), FailureReason.NO_API_KEY),
        (ReaderUnavailableError("the Claude API answered 529"), FailureReason.CLAUDE_UNAVAILABLE),
        (ReaderRefusedError("declined"), FailureReason.CLAUDE_UNAVAILABLE),
        (ReaderTruncatedError("cut off"), FailureReason.CLAUDE_UNAVAILABLE),
        (ReaderOutputError("not the schema"), FailureReason.CLAUDE_UNAVAILABLE),
        (_stopped(BudgetReachedError("reached")), FailureReason.BUDGET_REACHED),
        (UnsupportedMediaError("not an image"), FailureReason.UNREADABLE_PHOTO),
        (TimeoutError("timed out"), FailureReason.UNKNOWN),
        (ConnectionError("reset"), FailureReason.UNKNOWN),
        (ConflictError("receipt 1 has no stored photo"), FailureReason.UNKNOWN),
        (RuntimeError("boom"), FailureReason.UNKNOWN),
    ],
    ids=[
        "budget",
        "no-key",
        "api-error",
        "refused",
        "truncated",
        "malformed",
        "stopped-wraps-its-cause",
        "unsupported-photo",
        "timeout",
        "connection",
        "conflict",
        "anything-else",
    ],
)
def test_h2_01_each_failure_maps_to_its_reason(error: Exception, reason: FailureReason) -> None:
    assert failure_reason(error) is reason
