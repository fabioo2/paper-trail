"""``ProcessReceiptFile``: one image or text file in, one receipt and one graph run out.

Covers AC-A-02 (image dedup through the use case, S2-AC-03 pulled forward) and the use case's
own rules (agent-core plan 2.2).

Pinned (docs/tests/agent-core-A.md):
- ``ReceiptSource(name, image=None, text=None)`` raises ValueError unless exactly one is set;
- a new receipt is created PROCESSING/READING in one unit of work before the graph starts, with
  an empty store, currency CAD, no total, no items, ``image_path``/``image_sha256`` from the
  stored image (images) or the masked ``ocr_text`` (text);
- the same image bytes again return the first receipt with ``duplicate=True`` and
  ``graph=None``: nothing is stored again, the graph does not run, the LLM is not called;
  ``reprocess=True`` is slice C (``--reprocess``) and not covered here;
- a file that is not an image raises ``UnsupportedMediaError`` and creates no receipt.
"""

import pytest

from paper_trail.application.process_receipt import ProcessOutcome, ReceiptSource
from paper_trail.domain.enums import ProcessingStage, ReceiptStatus
from paper_trail.domain.errors import UnsupportedMediaError
from paper_trail.domain.paging import PageRequest
from tests.agent_harness import build_harness
from tests.extractions import COSTCO_OCR, MASKED_PAN, NOW, extraction
from tests.fakes.images import fake_png
from tests.fakes.llm import ScriptedLlm
from tests.pans import card_numbers


def test_receipt_source_needs_exactly_one_input() -> None:
    with pytest.raises(ValueError, match=r"image|text"):
        ReceiptSource(name="nothing.jpg")
    with pytest.raises(ValueError, match=r"image|text"):
        ReceiptSource(name="both.jpg", image=fake_png("x"), text="x")


def test_ac_a_02_same_image_twice_returns_the_first_receipt_without_a_new_run() -> None:
    """AC-A-02: duplicate=True, the first receipt id, no second graph run."""
    llm = ScriptedLlm(extraction())
    harness = build_harness(llm)

    first = harness.process_image("costco")
    second = harness.process_image("costco")

    assert first.duplicate is False
    assert second == ProcessOutcome(receipt_id=first.receipt_id, duplicate=True, graph=None)
    assert llm.calls == 1
    assert len(harness.images.files) == 1
    with harness.uow_factory(read_only=True) as uow:
        assert len(uow.receipts.list(PageRequest(limit=10)).items) == 1


def test_ac_a_02_different_images_are_different_receipts() -> None:
    harness = build_harness(ScriptedLlm(then=extraction()))

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
    harness = build_harness(ScriptedLlm(RuntimeError("model exploded")))

    outcome = harness.process_image("costco")

    receipt = harness.receipt(outcome.receipt_id)
    assert receipt.created_at == NOW
    stored = next(iter(harness.images.files))
    assert receipt.image_path == stored
    assert receipt.image_sha256 is not None
    assert stored.endswith(f"{receipt.image_sha256}.jpg")


def test_an_unexpected_error_fails_the_receipt_and_is_reported() -> None:
    """AC-A-10 (exit 3): the receipt is FAILED with an ERROR trace event; the use case
    returns the failure instead of raising, so a batch continues with the next file."""
    harness = build_harness(ScriptedLlm(RuntimeError("model exploded")))

    outcome = harness.process_image("costco")

    assert outcome.graph is None
    assert outcome.failure is not None
    assert "model exploded" in outcome.failure
    receipt = harness.receipt(outcome.receipt_id)
    assert (receipt.status, receipt.stage) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    assert harness.events(outcome.receipt_id)[-1].kind.value == "error"


def test_a_non_image_creates_no_receipt() -> None:
    harness = build_harness(ScriptedLlm())

    with pytest.raises(UnsupportedMediaError):
        harness.process(ReceiptSource(name="notes.jpg", image=b"%PDF-1.7 not an image"))

    with harness.uow_factory(read_only=True) as uow:
        assert uow.receipts.list(PageRequest(limit=10)).items == ()


def test_ac_a_11_text_is_masked_before_it_is_stored_or_sent() -> None:
    """AC-A-11: the text source's card number is masked before the receipt row is written."""
    harness = build_harness(ScriptedLlm(RuntimeError("stop after the receipt is created")))

    outcome = harness.process_text(COSTCO_OCR)

    receipt = harness.receipt(outcome.receipt_id)
    assert receipt.ocr_text is not None
    assert MASKED_PAN in receipt.ocr_text
    assert card_numbers(receipt.ocr_text) == []
    assert receipt.image_path is None
