"""The supervisor's routing is a pure function (S3-AC-06, ADR-0008).

Pinned (docs/tests/agent-core-A.md): ``route(facts, limits) -> RouteDecision`` in
``paper_trail.agents.routing``, where ``RoutingFacts(parsed, valid, confidence, text_attempts,
vision_attempts)`` describes the latest attempt and the attempts used so far, and
``RouteDecision(target, reason)`` carries the review reason (only) when the target is "review".
The plan's ``route(state, limits) -> Route`` is this, with the reason returned alongside.

Rules (PLAN 5): valid and confident -> persist; valid but below MIN_CONFIDENCE -> review
LOW_CONFIDENCE at once (a retry would not raise the model's confidence); invalid or unparsed
with text attempts left -> extract_text; then vision attempts left -> extract_vision; otherwise
review, VALIDATION_FAILED when the last attempt parsed, EXTRACTION_FAILED when it did not.

Extraction accuracy: ``GraphLimits`` gains ``vision_first_below`` (the first attempt's policy,
``effective_limits``; tested in ``test_vision_first.py``); ``route()`` itself is unchanged, so
these tables pass 0 (off).
"""

import itertools

import pytest

from paper_trail.agents.routing import GraphLimits, RouteDecision, RoutingFacts, route
from paper_trail.domain.enums import ReviewReason

LIMITS = GraphLimits(
    max_text_attempts=2,
    max_vision_attempts=1,
    min_confidence=0.6,
    feedback=True,
    vision_first_below=0.0,
)
NO_VISION = GraphLimits(
    max_text_attempts=2,
    max_vision_attempts=0,
    min_confidence=0.6,
    feedback=True,
    vision_first_below=0.0,
)
SINGLE = GraphLimits(
    max_text_attempts=1,
    max_vision_attempts=0,
    min_confidence=0.6,
    feedback=True,
    vision_first_below=0.0,
)

PERSIST = RouteDecision("persist", None)
RETRY_TEXT = RouteDecision("extract_text", None)
VISION = RouteDecision("extract_vision", None)
INVALID = RouteDecision("review", ReviewReason.VALIDATION_FAILED)
UNSURE = RouteDecision("review", ReviewReason.LOW_CONFIDENCE)
UNPARSED = RouteDecision("review", ReviewReason.EXTRACTION_FAILED)


def _facts(
    *,
    parsed: bool = True,
    valid: bool = True,
    confidence: float | None = 0.9,
    text: int = 1,
    vision: int = 0,
) -> RoutingFacts:
    return RoutingFacts(
        parsed=parsed,
        valid=valid,
        confidence=confidence if parsed else None,
        text_attempts=text,
        vision_attempts=vision,
    )


@pytest.mark.parametrize(
    ("facts", "limits", "expected"),
    [
        (_facts(), LIMITS, PERSIST),
        (_facts(confidence=0.6), LIMITS, PERSIST),  # the threshold itself is enough
        (_facts(confidence=0.59), LIMITS, UNSURE),
        (_facts(confidence=0.3, text=2), LIMITS, UNSURE),
        (_facts(text=2, vision=1), LIMITS, PERSIST),  # valid after the vision re-read
        (_facts(confidence=0.3, text=2, vision=1), LIMITS, UNSURE),
        (_facts(valid=False), LIMITS, RETRY_TEXT),
        (_facts(valid=False, confidence=0.3), LIMITS, RETRY_TEXT),  # invalid beats unsure
        (_facts(valid=False, text=2), LIMITS, VISION),
        (_facts(valid=False, text=2, vision=1), LIMITS, INVALID),
        (_facts(valid=False, confidence=0.3, text=2, vision=1), LIMITS, INVALID),
        (_facts(parsed=False, valid=False), LIMITS, RETRY_TEXT),
        (_facts(parsed=False, valid=False, text=2), LIMITS, VISION),
        (_facts(parsed=False, valid=False, text=2, vision=1), LIMITS, UNPARSED),
        (_facts(valid=False, text=2), NO_VISION, INVALID),
        (_facts(parsed=False, valid=False, text=2), NO_VISION, UNPARSED),
        (_facts(valid=False), SINGLE, INVALID),  # E1 "single attempt" arm
        (_facts(parsed=False, valid=False), SINGLE, UNPARSED),
        (_facts(), SINGLE, PERSIST),
    ],
)
def test_s3_ac_06_route_table(
    facts: RoutingFacts, limits: GraphLimits, expected: RouteDecision
) -> None:
    """S3-AC-06: the documented rows."""
    assert route(facts, limits) == expected


def _expected(facts: RoutingFacts, limits: GraphLimits) -> RouteDecision:
    """The rules of PLAN 5, written out independently of the implementation."""
    confidence = facts.confidence or 0.0
    if facts.parsed and facts.valid:
        return PERSIST if confidence >= limits.min_confidence else UNSURE
    if facts.text_attempts < limits.max_text_attempts:
        return RETRY_TEXT
    if facts.vision_attempts < limits.max_vision_attempts:
        return VISION
    return INVALID if facts.parsed else UNPARSED


ALL_LIMITS = [
    GraphLimits(
        max_text_attempts=t,
        max_vision_attempts=v,
        min_confidence=0.6,
        feedback=f,
        vision_first_below=0.0,
    )
    for t, v, f in itertools.product((1, 2, 3), (0, 1), (True, False))
]
ALL_FACTS = [
    _facts(parsed=parsed, valid=parsed and valid, confidence=confidence, text=text, vision=vision)
    for parsed, valid, confidence, text, vision in itertools.product(
        (True, False), (True, False), (0.0, 0.59, 0.6, 1.0), (1, 2, 3), (0, 1)
    )
]


@pytest.mark.parametrize("limits", ALL_LIMITS, ids=repr)
def test_s3_ac_06_every_combination(limits: GraphLimits) -> None:
    """S3-AC-06: every combination of validity, confidence, attempts and parse failure."""
    for facts in ALL_FACTS:
        if (
            facts.text_attempts > limits.max_text_attempts
            or facts.vision_attempts > limits.max_vision_attempts
        ):
            continue  # unreachable: the supervisor never allows more attempts than the caps
        assert route(facts, limits) == _expected(facts, limits), facts


@pytest.mark.parametrize("limits", ALL_LIMITS, ids=repr)
def test_s3_ac_06_caps_are_never_exceeded(limits: GraphLimits) -> None:
    """A retry is only ever routed while its attempt counter is below its cap."""
    for facts in ALL_FACTS:
        decision = route(facts, limits)
        if decision.target == "extract_text":
            assert facts.text_attempts < limits.max_text_attempts
        if decision.target == "extract_vision":
            assert facts.vision_attempts < limits.max_vision_attempts
        assert (decision.reason is not None) == (decision.target == "review")


def test_s3_ac_06_feedback_does_not_change_the_route() -> None:
    """``feedback`` only changes the retry prompt (E1 control arm), never where to go."""
    for facts in ALL_FACTS:
        with_feedback = GraphLimits(2, 1, 0.6, feedback=True, vision_first_below=0.0)
        without = GraphLimits(2, 1, 0.6, feedback=False, vision_first_below=0.0)
        assert route(facts, with_feedback) == route(facts, without)
