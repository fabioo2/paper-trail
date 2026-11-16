"""The receipt a graph thread saves, as a draft: what ``persist`` writes, what ``match``
matches, and what a review shows as pending."""

from dataclasses import replace

from paper_trail.agents.deps import AgentDeps
from paper_trail.agents.schemas import ReceiptAnswer
from paper_trail.agents.state import ReceiptState
from paper_trail.application.payloads import DraftPayload
from paper_trail.domain.models import ReceiptDraft
from paper_trail.domain.rules.readings import ReceiptReading, reading_draft


def saved_draft(deps: AgentDeps, state: ReceiptState) -> ReceiptDraft:
    """The person's draft after a review (with the graph's image when it has none), else the
    valid extraction."""
    answer = state.get("draft")
    if answer is not None:
        draft = DraftPayload.model_validate(answer).to_draft()
        image_path = state.get("image_path")
        return replace(
            draft, image_path=image_path if draft.image_path is None else draft.image_path
        )
    return extraction_draft(deps, state)


def extraction_reading(state: ReceiptState) -> ReceiptReading:
    """The latest extraction in cents (it parsed, or it would not be in the state)."""
    return ReceiptAnswer.model_validate(state.get("extraction")).to_reading()


def extraction_draft(deps: AgentDeps, state: ReceiptState) -> ReceiptDraft:
    """The latest extraction as a draft (``reading_draft``), its date read relative to today."""
    return reading_draft(
        extraction_reading(state),
        today=deps.clock.now().date(),
        image_path=state.get("image_path"),
    )
