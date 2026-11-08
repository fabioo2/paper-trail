"""The receipt a graph thread saves, as a draft: what ``persist`` writes, what ``match``
matches, and what a review shows as pending."""

from dataclasses import replace

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.schemas import ExtractionOutput
from paper_trail.agents.state import ReceiptState
from paper_trail.application.payloads import DraftPayload
from paper_trail.domain.models import ReceiptDraft


def saved_draft(deps: AgentDeps, state: ReceiptState) -> ReceiptDraft:
    """The person's draft after a review (with the graph's OCR text and image when it has
    none), else the valid extraction."""
    ocr_text, image_path = state.get("ocr_text"), state.get("image_path")
    answer = state.get("draft")
    if answer is not None:
        draft = DraftPayload.model_validate(answer).to_draft()
        return replace(
            draft,
            ocr_text=ocr_text if draft.ocr_text is None else draft.ocr_text,
            image_path=image_path if draft.image_path is None else draft.image_path,
        )
    return extraction_draft(deps, state)


def extraction_draft(deps: AgentDeps, state: ReceiptState) -> ReceiptDraft:
    """The latest extraction as a draft, its date read relative to today."""
    return ExtractionOutput.model_validate(state.get("extraction")).to_draft(
        today=deps.clock.now().date(),
        ocr_text=state.get("ocr_text"),
        image_path=state.get("image_path"),
    )
