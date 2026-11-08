"""The supervisor's routing: pure functions of the latest attempt and the limits (ADR-0008,
ADR-0017).

Valid and confident -> persist. Valid but below the confidence threshold -> review at once (a
retry would not make the model surer). Invalid or unparsed -> retry on the OCR text while text
attempts remain, then re-read the image while vision attempts remain, then review.

The limits of one receipt depend on its input (``effective_limits``): a text input is never sent
to vision, and a photo whose OCR read is poor goes to vision first, with no text attempts.
"""

from dataclasses import dataclass, replace
from typing import Literal

from paper_trail.domain.enums import ReviewReason

ALWAYS_VISION_FIRST = 1.0  # a vision_first_below that sends every photo to vision first

type Target = Literal["extract_text", "extract_vision", "review", "persist"]


@dataclass(frozen=True, slots=True)
class GraphLimits:
    max_text_attempts: int
    max_vision_attempts: int
    min_confidence: float
    feedback: bool  # retries carry the issues; never changes the route
    # OCR mean confidence below which a photo goes to vision first; 1.0: every photo; 0: off
    vision_first_below: float


@dataclass(frozen=True, slots=True)
class RoutingFacts:
    parsed: bool  # the latest answer matched the schema
    valid: bool  # ... and adds up
    confidence: float | None  # the model's, when parsed
    text_attempts: int
    vision_attempts: int


@dataclass(frozen=True, slots=True)
class RouteDecision:
    target: Target
    reason: ReviewReason | None  # set only when the target is "review"


def route(facts: RoutingFacts, limits: GraphLimits) -> RouteDecision:
    if facts.parsed and facts.valid:
        if (facts.confidence or 0.0) >= limits.min_confidence:
            return RouteDecision("persist", None)
        return RouteDecision("review", ReviewReason.LOW_CONFIDENCE)
    if facts.text_attempts < limits.max_text_attempts:
        return RouteDecision("extract_text", None)
    if facts.vision_attempts < limits.max_vision_attempts:
        return RouteDecision("extract_vision", None)
    reason = ReviewReason.VALIDATION_FAILED if facts.parsed else ReviewReason.EXTRACTION_FAILED
    return RouteDecision("review", reason)


def effective_limits(
    limits: GraphLimits, *, has_photo: bool, ocr_confidence: float | None
) -> GraphLimits:
    """The limits for one receipt: without a photo no vision attempt; a photo read below
    ``vision_first_below`` (vision allowed) gets no text attempts. At 1.0 every photo does,
    whatever its read (a perfect one, or one without a confidence)."""
    if not has_photo:
        return replace(limits, max_vision_attempts=0)
    always = limits.vision_first_below >= ALWAYS_VISION_FIRST
    poor_read = ocr_confidence is not None and ocr_confidence < limits.vision_first_below
    if (always or poor_read) and limits.max_vision_attempts > 0:
        return replace(limits, max_text_attempts=0)
    return limits


def first_attempt(limits: GraphLimits) -> Target:
    """Where a receipt's first extraction attempt goes."""
    return "extract_text" if limits.max_text_attempts > 0 else "extract_vision"
