"""Vision first on a poor OCR read, and a clean first vision prompt (EA-AC-08, EA-AC-09).

docs/plans/extraction-accuracy.md 2.6. Scripted LLM, in-process tools, fakes; the OCR
confidence is set on the harness's ``FakeOcr`` before the photo is processed.

Pinned:
- ``GraphLimits`` gains ``vision_first_below: float`` (0 disables; from
  ``GRAPH__VISION_FIRST_BELOW``). ``effective_limits(limits, *, has_photo, ocr_confidence)`` in
  ``paper_trail.agents.routing``: no photo -> ``max_vision_attempts=0``; a photo, vision
  allowed and ``ocr_confidence < vision_first_below`` -> ``max_text_attempts=0``; otherwise
  unchanged. ``first_attempt(limits)`` is "extract_text" while text attempts are allowed, else
  "extract_vision". ``route()`` is unchanged.
- The graph: ``read`` -> ``first_attempt(effective_limits(...))``. Going vision first emits one
  ROUTE event named "extract_vision" before any model call; a text-first photo emits no extra
  ROUTE event (the existing route lists of every suite stay as they are).
- The first vision attempt's request has no ``<validation_feedback>``, whatever the text
  attempts before it said; a vision retry carries only the issues (or parse error) of the
  previous vision answer.
"""

from dataclasses import replace

import pytest

from paper_trail.agents.routing import (
    GraphLimits,
    RouteDecision,
    RoutingFacts,
    effective_limits,
    first_attempt,
    route,
)
from paper_trail.application.process_receipt import ProcessOutcome
from paper_trail.domain.enums import ReceiptStatus, ReviewReason
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.agents import GraphOutcome
from tests.agent_harness import TEXT_MODEL, VISION_MODEL, Harness, agent_settings, build_harness
from tests.extractions import (
    COSTCO_OCR,
    GENERIC_RETRY_LINE,
    LINE1_ISSUE,
    LINE3_ISSUE,
    SUBTOTAL_ISSUE,
    printed_extraction,
    printed_inconsistent_extraction,
)
from tests.fakes.images import fake_png, turned
from tests.fakes.llm import Reply, ScriptedLlm

LABEL = "crumpled-costco"
PHOTO = fake_png(LABEL)
POOR_OCR = 0.40  # the real crumpled photos read at 0.41-0.49 (ADR-0010)
GOOD_OCR = 0.87  # FakeOcr's default
THRESHOLD = 0.65  # GRAPH__VISION_FIRST_BELOW's default
FEEDBACK_TAG = "<validation_feedback>"

LIMITS = GraphLimits(
    max_text_attempts=2,
    max_vision_attempts=2,
    min_confidence=0.6,
    feedback=True,
    vision_first_below=THRESHOLD,
)
NO_VISION = GraphLimits(
    max_text_attempts=2,
    max_vision_attempts=0,
    min_confidence=0.6,
    feedback=True,
    vision_first_below=THRESHOLD,
)
NEVER_FIRST = GraphLimits(
    max_text_attempts=2,
    max_vision_attempts=2,
    min_confidence=0.6,
    feedback=True,
    vision_first_below=0.0,
)
TEXT_ONLY = GraphLimits(
    max_text_attempts=2,
    max_vision_attempts=0,
    min_confidence=0.6,
    feedback=True,
    vision_first_below=THRESHOLD,
)
ALWAYS = GraphLimits(
    max_text_attempts=2,
    max_vision_attempts=2,
    min_confidence=0.6,
    feedback=True,
    vision_first_below=1.0,
)
ALWAYS_VISION_ONLY = GraphLimits(
    max_text_attempts=0,
    max_vision_attempts=2,
    min_confidence=0.6,
    feedback=True,
    vision_first_below=1.0,
)
ALWAYS_TEXT_ONLY = GraphLimits(
    max_text_attempts=2,
    max_vision_attempts=0,
    min_confidence=0.6,
    feedback=True,
    vision_first_below=1.0,
)
VISION_ONLY = GraphLimits(
    max_text_attempts=0,
    max_vision_attempts=2,
    min_confidence=0.6,
    feedback=True,
    vision_first_below=THRESHOLD,
)


# --- EA-AC-08: the routing table -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("limits", "has_photo", "confidence", "expected"),
    [
        (LIMITS, False, None, TEXT_ONLY),  # a text input
        (LIMITS, False, POOR_OCR, TEXT_ONLY),  # no photo: never vision
        (LIMITS, True, POOR_OCR, VISION_ONLY),  # a poor read of a photo: vision first
        (LIMITS, True, 0.0, VISION_ONLY),
        (LIMITS, True, THRESHOLD, LIMITS),  # at the threshold: text first
        (LIMITS, True, GOOD_OCR, LIMITS),
        (LIMITS, True, None, LIMITS),  # no confidence known: text first
        (NEVER_FIRST, True, POOR_OCR, NEVER_FIRST),  # threshold 0: off
        (NEVER_FIRST, True, 0.0, NEVER_FIRST),
        (NO_VISION, True, POOR_OCR, NO_VISION),  # vision disabled (--no-vision): text only
        (ALWAYS, True, 1.0, ALWAYS_VISION_ONLY),  # 1.0: every photo, even a perfect read
        (ALWAYS, True, None, ALWAYS_VISION_ONLY),  # ... and one with no confidence
        (ALWAYS, True, GOOD_OCR, ALWAYS_VISION_ONLY),
        (ALWAYS, False, None, ALWAYS_TEXT_ONLY),  # a text input still uses the text path
        (replace(ALWAYS, max_vision_attempts=0), True, 1.0, ALWAYS_TEXT_ONLY),
    ],
    ids=[
        "text-input",
        "no-photo",
        "photo-below",
        "photo-zero",
        "photo-at-threshold",
        "photo-good",
        "photo-no-confidence",
        "threshold-0",
        "threshold-0-zero",
        "vision-disabled",
        "always-perfect-read",
        "always-no-confidence",
        "always-good",
        "always-text-input",
        "always-vision-disabled",
    ],
)
def test_ea_ac_08_effective_limits(
    limits: GraphLimits, has_photo: bool, confidence: float | None, expected: GraphLimits
) -> None:
    assert effective_limits(limits, has_photo=has_photo, ocr_confidence=confidence) == expected


def test_ea_ac_08_the_first_attempt() -> None:
    assert first_attempt(LIMITS) == "extract_text"
    assert first_attempt(TEXT_ONLY) == "extract_text"
    assert first_attempt(VISION_ONLY) == "extract_vision"


@pytest.mark.parametrize(
    ("parsed", "vision", "expected"),
    [
        (False, 1, RouteDecision("extract_vision", None)),
        (True, 1, RouteDecision("extract_vision", None)),
        (False, 2, RouteDecision("review", ReviewReason.EXTRACTION_FAILED)),
        (True, 2, RouteDecision("review", ReviewReason.VALIDATION_FAILED)),
    ],
)
def test_ea_ac_08_route_with_no_text_attempts_goes_to_vision_then_review(
    parsed: bool, vision: int, expected: RouteDecision
) -> None:
    facts = RoutingFacts(
        parsed=parsed,
        valid=False,
        confidence=0.9 if parsed else None,
        text_attempts=0,
        vision_attempts=vision,
    )

    assert route(facts, VISION_ONLY) == expected


# --- EA-AC-08: the graph -------------------------------------------------------------------------


def _harness(
    *replies: Reply,
    then: Reply | None = None,
    ocr_confidence: float | None = GOOD_OCR,
    max_vision_attempts: int = 2,
    feedback: bool = True,
    vision_first_below: float | None = THRESHOLD,
) -> Harness:
    """Production-like: extraction/v2, two vision attempts, enrichment off; vision first below
    0.65 (the threshold these tests are about; production sends every photo, see the end)."""
    settings = agent_settings(
        extraction_version="v2",
        max_vision_attempts=max_vision_attempts,
        feedback=feedback,
        vision_first_below=vision_first_below,
    )
    harness = build_harness(ScriptedLlm(*replies, then=then), settings=settings)
    harness.ocr.mean_confidence = ocr_confidence
    return harness


def _graph(outcome: ProcessOutcome) -> GraphOutcome:
    assert outcome.failure is None, outcome.failure
    assert outcome.graph is not None
    return outcome.graph


def _routes(harness: Harness, receipt_id: int) -> list[str]:
    return [name for kind, name in harness.steps(receipt_id) if kind is TraceKind.ROUTE]


def _models(harness: Harness) -> list[str]:
    return [request.model for request in harness.llm.requests]


def test_ea_ac_08_a_poor_read_goes_straight_to_vision_and_then_review() -> None:
    harness = _harness(then=printed_inconsistent_extraction(), ocr_confidence=POOR_OCR)

    graph = _graph(harness.process_image(LABEL))

    assert _models(harness) == [VISION_MODEL, VISION_MODEL]
    assert (graph.text_attempts, graph.vision_attempts) == (0, 2)
    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    assert graph.review_reason is ReviewReason.VALIDATION_FAILED
    runs = harness.node_runs(graph.receipt_id)
    assert runs[:2] == ["read", "extract_vision"]
    assert "extract_text" not in runs
    assert _routes(harness, graph.receipt_id) == ["extract_vision", "extract_vision", "review"]


def test_ea_ac_08_the_vision_first_route_is_traced_before_any_model_call() -> None:
    harness = _harness(printed_extraction(), ocr_confidence=POOR_OCR)

    graph = _graph(harness.process_image(LABEL))

    kinds = [kind for kind, _ in harness.steps(graph.receipt_id)]
    first_route = kinds.index(TraceKind.ROUTE)
    assert first_route < kinds.index(TraceKind.LLM)
    assert harness.steps(graph.receipt_id)[first_route] == (TraceKind.ROUTE, "extract_vision")


def test_ea_ac_08_a_vision_first_read_that_adds_up_is_saved() -> None:
    harness = _harness(printed_extraction(), ocr_confidence=POOR_OCR)

    graph = _graph(harness.process_image(LABEL))

    assert graph.status is ReceiptStatus.COMPLETED
    assert (graph.text_attempts, graph.vision_attempts) == (0, 1)
    assert harness.llm.calls == 1
    assert harness.receipt(graph.receipt_id).total is not None


def test_ea_ac_08_the_vision_first_request_is_the_photo_alone() -> None:
    harness = _harness(printed_extraction(), ocr_confidence=POOR_OCR)

    harness.process_image(LABEL)

    (vision,) = harness.llm.requests
    assert vision.tag == "vision/v1"
    assert [message.role for message in vision.messages] == ["system", "user"]
    assert tuple(vision.messages[-1].images) == (PHOTO,)
    assert FEEDBACK_TAG not in vision.messages[-1].content
    assert all("KS ORG EGGS" not in message.content for message in vision.messages)


@pytest.mark.parametrize(
    ("confidence", "max_vision_attempts", "vision_first_below"),
    [
        (THRESHOLD, 2, THRESHOLD),  # at the threshold
        (GOOD_OCR, 2, THRESHOLD),
        (None, 2, THRESHOLD),  # the OCR reported no confidence
        (POOR_OCR, 0, None),  # vision disabled (whatever the threshold, the production one)
        (POOR_OCR, 2, 0.0),  # vision first switched off
    ],
    ids=["at-threshold", "good", "no-confidence", "vision-disabled", "threshold-0"],
)
def test_ea_ac_08_otherwise_a_photo_starts_with_the_text_model(
    confidence: float | None, max_vision_attempts: int, vision_first_below: float | None
) -> None:
    harness = _harness(
        printed_extraction(),
        ocr_confidence=confidence,
        max_vision_attempts=max_vision_attempts,
        vision_first_below=vision_first_below,
    )

    graph = _graph(harness.process_image(LABEL))

    assert _models(harness) == [TEXT_MODEL]
    assert harness.node_runs(graph.receipt_id)[:2] == ["read", "extract_text"]
    assert _routes(harness, graph.receipt_id) == ["persist"]  # no ROUTE event for text first


def test_ea_ac_08_a_text_input_is_unaffected() -> None:
    harness = _harness(printed_extraction(), ocr_confidence=POOR_OCR)

    graph = _graph(harness.process_text(COSTCO_OCR))

    assert _models(harness) == [TEXT_MODEL]
    assert graph.status is ReceiptStatus.COMPLETED


# --- EA-AC-09: what each vision request carries --------------------------------------------------


def test_ea_ac_09_the_first_vision_request_after_text_attempts_has_no_feedback() -> None:
    harness = _harness(
        printed_inconsistent_extraction(),
        printed_inconsistent_extraction(),
        printed_inconsistent_extraction(line=1),
        printed_extraction(),
    )

    graph = _graph(harness.process_image(LABEL))

    assert _models(harness) == [TEXT_MODEL, TEXT_MODEL, VISION_MODEL, VISION_MODEL]
    assert LINE3_ISSUE in harness.llm.last_user_message(1)  # the text retry still gets it
    first_vision = harness.llm.requests[2]
    assert all(FEEDBACK_TAG not in message.content for message in first_vision.messages)
    assert all(LINE3_ISSUE not in message.content for message in first_vision.messages)
    second_vision = harness.llm.last_user_message(3)
    assert FEEDBACK_TAG in second_vision
    assert LINE1_ISSUE in second_vision
    assert SUBTOTAL_ISSUE in second_vision
    assert LINE3_ISSUE not in second_vision  # only the vision answer's own issues
    assert graph.status is ReceiptStatus.COMPLETED
    assert (graph.text_attempts, graph.vision_attempts) == (2, 2)


def test_ea_ac_09_without_feedback_the_first_vision_request_is_still_clean() -> None:
    harness = _harness(
        printed_inconsistent_extraction(),
        printed_inconsistent_extraction(),
        printed_inconsistent_extraction(),
        printed_extraction(),
        feedback=False,
    )

    harness.process_image(LABEL)

    assert FEEDBACK_TAG not in harness.llm.last_user_message(2)
    assert GENERIC_RETRY_LINE not in harness.llm.last_user_message(2)
    assert GENERIC_RETRY_LINE in harness.llm.last_user_message(3)


def test_ea_ac_09_a_vision_retry_carries_the_vision_answers_parse_error() -> None:
    """EA-AC-05 on the vision path: the printed-value error is the next attempt's feedback."""
    ten_x = printed_extraction()
    ten_x["items"][0]["amount"] = "899"
    harness = _harness(ten_x, printed_extraction(), ocr_confidence=POOR_OCR)

    graph = _graph(harness.process_image(LABEL))

    retry = harness.llm.last_user_message(1)
    assert FEEDBACK_TAG in retry
    assert '"899"' in retry
    assert "line 1" in retry
    assert graph.status is ReceiptStatus.COMPLETED
    assert (graph.text_attempts, graph.vision_attempts) == (0, 2)


# --- the photo as the OCR read it, and a bounded answer (real run 2026-11-08) --------------------


@pytest.mark.parametrize(("rotation", "image"), [(90, PHOTO + turned(90)), (0, PHOTO)])
def test_the_vision_model_sees_the_photo_turned_like_the_ocr(rotation: int, image: bytes) -> None:
    """A sideways photo made the vision model loop; the OCR's rotation turns it upright."""
    harness = _harness(printed_extraction(), ocr_confidence=POOR_OCR)
    harness.ocr.rotation_degrees = rotation

    harness.process_image(LABEL)

    (vision,) = harness.llm.requests
    assert tuple(vision.messages[-1].images) == (image,)


def test_a_vision_retry_after_text_attempts_is_turned_too() -> None:
    harness = _harness(
        printed_inconsistent_extraction(), printed_inconsistent_extraction(), printed_extraction()
    )
    harness.ocr.rotation_degrees = 270

    harness.process_image(LABEL)

    assert tuple(harness.llm.requests[2].messages[-1].images) == (PHOTO + turned(270),)


def test_every_extraction_request_bounds_its_output() -> None:
    harness = _harness(
        printed_inconsistent_extraction(), printed_inconsistent_extraction(), printed_extraction()
    )

    harness.process_image(LABEL)

    bound = harness.settings.ollama.max_output_tokens
    assert [request.max_tokens for request in harness.llm.requests] == [bound] * 3


# --- real run 2026-11-08 (Superstore): the multi-buy the vision model missed ---------------------

MULTI_BUY_OCR = """REAL CANADIAN SUPERSTORE
KS ORG EGGS 24CT             8.99
(1)05700001285 CRV CHKN PBLN BR MRJ
(1)05700001299 CRV CRN SD BRRT MRJ
$5.99 ea or 2/$10.00 KB
2 @ 2/$10.00                10.00
SUBTOTAL                    18.99
TOTAL                       18.99
"""


def _missed_multi_buy() -> dict[str, object]:
    """The vision answer: both products at the offer's 5.99, and a tax that is not printed."""
    answer = printed_extraction(subtotal="18.99", taxes=["8.99 H"], total="18.99")
    eggs = answer["items"][0]
    answer["items"] = [
        eggs,
        {
            **eggs,
            "raw_text": "(1)05700001285 CRV CHKN PBLN BR",
            "description": "Chicken",
            "quantity": None,
            "unit_price": "$5.99 ea",
            "amount": "5.99",
        },
        {
            **eggs,
            "raw_text": "(1)05700001299 CRV CRN SD BRRT",
            "description": "Burrito",
            "quantity": None,
            "unit_price": "$5.99 ea",
            "amount": "5.99",
        },
    ]
    return answer


def test_real_run_a_missed_multi_buy_is_reconciled_from_the_ocr_text() -> None:
    harness = _harness(_missed_multi_buy(), ocr_confidence=POOR_OCR)
    harness.ocr.text = MULTI_BUY_OCR

    graph = _graph(harness.process_image(LABEL))

    assert graph.status is ReceiptStatus.COMPLETED
    assert harness.llm.calls == 1
    receipt = harness.receipt(graph.receipt_id)
    assert [line.line_total.amount_cents for line in receipt.items] == [899, 500, 500]
    assert receipt.tax is not None
    assert receipt.tax.amount_cents == 0
    notes = [event for event in harness.events(graph.receipt_id) if event.kind is TraceKind.NOTE]
    assert [note.summary for note in notes] == [
        "tax 8.99 H ignored: subtotal equals total",
        "reconciled multi-buy 2 @ 2/$10.00 from OCR",
    ]


def test_real_run_an_ignored_tax_is_a_visible_note() -> None:
    answer = printed_extraction(subtotal="26.83", taxes=["8.99 H"], total="26.83")
    answer["items"][0]["amount"] = answer["items"][0]["unit_price"] = "10.27"
    harness = _harness(answer, ocr_confidence=POOR_OCR)

    graph = _graph(harness.process_image(LABEL))

    assert graph.status is ReceiptStatus.COMPLETED
    notes = [event for event in harness.events(graph.receipt_id) if event.kind is TraceKind.NOTE]
    assert [(note.name, note.summary) for note in notes] == [
        ("tax", "tax 8.99 H ignored: subtotal equals total")
    ]


def test_real_run_a_printed_tax_is_no_note() -> None:
    harness = _harness(printed_extraction(), ocr_confidence=POOR_OCR)

    graph = _graph(harness.process_image(LABEL))

    assert not [e for e in harness.events(graph.receipt_id) if e.kind is TraceKind.NOTE]


def test_real_run_without_the_ocr_line_nothing_is_reconciled() -> None:
    harness = _harness(then=_missed_multi_buy(), ocr_confidence=POOR_OCR)
    harness.ocr.text = MULTI_BUY_OCR.replace("2 @ 2/$10.00                10.00\n", "")

    graph = _graph(harness.process_image(LABEL))

    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    notes = [e.name for e in harness.events(graph.receipt_id) if e.kind is TraceKind.NOTE]
    assert "reconcile" not in notes


# --- blind test 2026-11-08: a new Superstore receipt, the tax copied as base and rate -----------


def test_blind_test_a_tax_computed_from_base_and_rate_completes_with_a_note() -> None:
    amounts = ["2.99", "3.49", "4.29", "1.99", "5.49", "3.79", "2.67", "3.99"]  # 28.70
    answer = printed_extraction(subtotal="28.70", taxes=["6.49 @ 13.000%"], total="29.54")
    template = answer["items"][0]
    answer["items"] = [
        {**template, "raw_text": f"ITEM {n}", "quantity": None, "unit_price": None, "amount": a}
        for n, a in enumerate(amounts, start=1)
    ]
    harness = _harness(answer, ocr_confidence=POOR_OCR)

    graph = _graph(harness.process_image(LABEL))

    assert graph.status is ReceiptStatus.COMPLETED
    receipt = harness.receipt(graph.receipt_id)
    assert receipt.tax is not None
    assert receipt.tax.amount_cents == 84
    notes = [event for event in harness.events(graph.receipt_id) if event.kind is TraceKind.NOTE]
    assert [(note.name, note.summary) for note in notes] == [
        ("tax", "tax 0.84 computed from 6.49 @ 13.000%")
    ]


# --- completeness (owner decision 2026-11-08): no date or no store is not COMPLETED -------------

NO_DATE = "no purchase date was read: copy the date exactly as printed"


def test_completeness_a_dateless_vision_answer_retries_with_feedback_then_completes() -> None:
    harness = _harness(
        printed_extraction(purchased_on=None), printed_extraction(), ocr_confidence=POOR_OCR
    )

    graph = _graph(harness.process_image(LABEL))

    assert graph.status is ReceiptStatus.COMPLETED
    assert (graph.text_attempts, graph.vision_attempts) == (0, 2)
    retry = harness.llm.last_user_message(1)
    assert FEEDBACK_TAG in retry
    assert NO_DATE in retry
    assert harness.receipt(graph.receipt_id).purchased_on is not None


def test_completeness_two_dateless_answers_go_to_review() -> None:
    harness = _harness(then=printed_extraction(purchased_on=None), ocr_confidence=POOR_OCR)

    graph = _graph(harness.process_image(LABEL))

    assert graph.status is ReceiptStatus.NEEDS_REVIEW
    assert graph.review_reason is ReviewReason.VALIDATION_FAILED
    assert harness.llm.calls == 2


def test_completeness_a_blank_store_is_not_complete() -> None:
    harness = _harness(
        printed_extraction(store_name=" "), printed_extraction(), ocr_confidence=POOR_OCR
    )

    graph = _graph(harness.process_image(LABEL))

    assert graph.status is ReceiptStatus.COMPLETED
    assert "no store name was read" in harness.llm.last_user_message(1)


@pytest.mark.parametrize("confidence", [GOOD_OCR, 1.0, None])
def test_vision_first_by_default_for_every_photo(confidence: float | None) -> None:
    """GRAPH__VISION_FIRST_BELOW defaults to 1.0: a photo always goes to vision first."""
    settings = agent_settings(
        extraction_version="v2", max_vision_attempts=2, vision_first_below=None
    )
    assert settings.graph.vision_first_below == 1.0
    harness = build_harness(ScriptedLlm(printed_extraction()), settings=settings)
    harness.ocr.mean_confidence = confidence

    graph = _graph(harness.process_image(LABEL))

    assert _models(harness) == [VISION_MODEL]
    assert graph.status is ReceiptStatus.COMPLETED
