"""The receipt graph end to end over fakes with Claude reading the photo (claude-extraction plan
TF, E1-E4, V11; CE-TC-021-026, 030, 032, 080). Everything goes through ``ProcessReceiptFile``
with the scripted reader, the in-process tool gateway and fakes.

Pinned:
- attempt 1 reads on ``CLAUDE__EXTRACT_MODEL`` with no feedback; a failed attempt (any E2 rule,
  a missing store or date, an unreadable value, an answer that fails the schema) is retried
  once on ``CLAUDE__RETRY_MODEL`` with every failure listed in the request's text; failing
  twice parks the receipt for review (``validation_failed``);
- every read sends the stored photo as stored;
- a refusal, a truncated answer, an API error or a timeout stops the run: a ``ReaderError``
  propagates, the receipt is FAILED/FAILED with its checkpoint, an ERROR event names the error
  class and message, nothing else is written, and the retry model is never asked;
- the extraction prompt says "Copy every price exactly as printed." and "Never adjust a value
  to make the totals match." on every read;
- card numbers in Claude's answer are masked before they reach state, storage, reviews or the
  trace.

In-process tools; the MCP transport is compared in ``test_transport_equivalence.py``.
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from paper_trail.adapters.catalog.plu import PluCatalog
from paper_trail.agents.nodes.extraction import MAX_OUTPUT_TOKENS
from paper_trail.agents.schemas import ReceiptAnswer
from paper_trail.domain.enums import (
    AgentRole,
    Category,
    CodeScheme,
    FieldSource,
    LineKind,
    ProcessingStage,
    ReceiptStatus,
    ReviewReason,
    ReviewStatus,
)
from paper_trail.domain.money import Money
from paper_trail.domain.rules.readings import check_reading
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.reader import (
    ReaderError,
    ReaderRefusedError,
    ReaderTruncatedError,
    ReaderUnavailableError,
)
from tests.agent_harness import EXTRACT_MODEL, RETRY_MODEL, Harness, agent_settings, build_harness
from tests.extractions import (
    C1,
    L3,
    L4,
    METRO_PURCHASED_ON,
    METRO_TOTAL_CENTS,
    PLU_CSV,
    STORE_NAME,
    adjustment,
    answer,
    coded_answer,
    inconsistent_answer,
    item,
    metro_answer,
    receipt_answer,
)
from tests.factories import TEST_PAN
from tests.fakes.images import fake_png
from tests.fakes.reader import ScriptedReader
from tests.fakes.tools import DenyingToolGateway
from tests.pans import card_numbers
from tests.support import require_id
from tests.unit.agents.graph_support import (
    graph,
    raw_traced,
    review_messages,
    saved_values,
    scripted,
)

COPY_AS_PRINTED = "Copy every price exactly as printed."
NEVER_ADJUST = "Never adjust a value to make the totals match."
ALL_NODES = [
    "extract",
    "validate",
    "supervisor",
    "persist",
    "match",
    "commit_matches",
    "enrich",
    "commit_enrichment",
]
# The Costco milk misread of the plan's test (6.05 for 6.09): here 6.54 for 6.58, 4 cents short.
MILK_MISREAD = "6.54"
GTIN = "036000291452"


def _milk_misread() -> dict[str, Any]:
    value = answer()
    value["items"][1]["regular_price"] = MILK_MISREAD
    value["items"][1]["price_paid"] = MILK_MISREAD
    return value


def _failures(reply: dict[str, Any]) -> list[str]:
    """What the validator says about ``reply``: the retry's feedback, verbatim."""
    reading = ReceiptAnswer.model_validate_json(json.dumps(reply)).to_reading()
    return [issue.message for issue in check_reading(reading).issues]


def _feedback_free_text() -> str:
    """The text of a first read, which carries no feedback."""
    harness = build_harness(ScriptedReader(answer()))
    graph(harness.process_image())
    return scripted(harness).requests[0].text


def _prompt(harness: Harness, call: int) -> str:
    request = scripted(harness).requests[call]
    return f"{request.system}\n{request.text}"


# --- CE-TC-021: first attempt, extract model, no feedback ---------------------------------------


def test_ce_tc_021_a_valid_receipt_is_read_once_on_the_extract_model() -> None:
    harness = build_harness(
        ScriptedReader(metro_answer()), settings=agent_settings(enrichment=True)
    )

    outcome = harness.process_image("metro")

    result = graph(outcome)
    assert (result.status, result.attempts, result.review_id) == (
        ReceiptStatus.COMPLETED,
        1,
        None,
    )
    (request,) = scripted(harness).requests
    assert (request.model, request.max_tokens) == (EXTRACT_MODEL, MAX_OUTPUT_TOKENS)
    assert MAX_OUTPUT_TOKENS == 16000
    assert request.image == fake_png("metro")
    assert harness.node_runs(outcome.receipt_id) == ALL_NODES
    receipt = harness.receipt(outcome.receipt_id)
    assert (receipt.status, receipt.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert (receipt.store_key, receipt.purchased_on, receipt.total) == (
        "metro",
        METRO_PURCHASED_ON,
        Money(METRO_TOTAL_CENTS, "CAD"),
    )
    assert receipt.ocr_text is None
    assert harness.open_reviews() == ()


def test_ce_tc_021_the_department_gives_each_new_product_its_category() -> None:
    """The stored lines are owned by ``test_metro_run_sqlite.py`` and ``test_readings.py``
    (CE-TC-052, 078); here only the department hint reaching the products."""
    harness = build_harness(ScriptedReader(metro_answer()))

    receipt = harness.receipt(graph(harness.process_image("metro")).receipt_id)

    categories = {
        line.raw_text: harness.product(require_id(line.product_id)).category
        for line in receipt.items
        if line.kind is LineKind.ITEM
    }
    assert categories == {
        "BANANAS": Category.PRODUCE,
        "ROMAINE HEARTS": Category.PRODUCE,
        "WHOLE GRAIN CRACKERS": None,
        "TOMATOES ON VINE": Category.PRODUCE,
        "DAILY SHAMPOO": Category.PERSONAL_CARE,
        "PASTA SAUCE": None,
        "TOOTHPASTE 4PK": Category.PERSONAL_CARE,
    }


# --- CE-TC-022, 023, 025: the retry on the retry model, then review -------------------------------


def test_ce_tc_022_a_failed_validation_is_retried_on_the_retry_model_with_the_failures() -> None:
    misread = _milk_misread()
    harness = build_harness(ScriptedReader(misread, answer()))

    outcome = harness.process_image()

    result = graph(outcome)
    assert (result.status, result.attempts, result.review_id) == (
        ReceiptStatus.COMPLETED,
        2,
        None,
    )
    reader = scripted(harness)
    assert reader.models == [EXTRACT_MODEL, RETRY_MODEL]
    first, retry = reader.requests
    failures = _failures(misread)
    assert failures
    for failure in failures:
        assert failure in retry.text
        assert failure not in first.text
    assert "25.51" in retry.text
    assert "25.55" in retry.text
    assert retry.image == first.image
    assert harness.receipt(outcome.receipt_id).items[1].line_total == Money(658, "CAD")
    assert harness.open_reviews() == ()


def _soda_12pk() -> dict[str, Any]:
    """Ruling C3: the pack size read as a quantity (12 x 8.99 for a line of 8.99)."""
    return receipt_answer(
        [item("KS SODA 12PK", "8.99", quantity="12", unit_price="8.99")], total="8.99"
    )


def test_c3_a_pack_size_read_as_a_quantity_is_retried_then_reviewed() -> None:
    """The each item's quantity x unit price is checked (CQ21's weighed-only rule is gone);
    a correct multi-quantity each item (the milk, 2 x 3.29) passes in CE-TC-021."""
    harness = build_harness(ScriptedReader(_soda_12pk(), _soda_12pk()))

    result = graph(harness.process_image())

    reader = scripted(harness)
    assert (reader.models, result.status, result.review_reason) == (
        [EXTRACT_MODEL, RETRY_MODEL],
        ReceiptStatus.NEEDS_REVIEW,
        ReviewReason.VALIDATION_FAILED,
    )
    assert "KS SODA 12PK" in reader.requests[1].text


def test_ce_tc_023_failing_twice_parks_the_receipt_for_review_with_the_second_reading() -> None:
    second_reading = inconsistent_answer()
    second_reading["items"][0]["price_paid"] = second_reading["items"][0]["regular_price"] = "8.90"
    harness = build_harness(ScriptedReader(_milk_misread(), second_reading))

    outcome = harness.process_image()

    result = graph(outcome)
    assert scripted(harness).models == [EXTRACT_MODEL, RETRY_MODEL]
    assert (result.status, result.review_reason, result.attempts, result.paused_at) == (
        ReceiptStatus.NEEDS_REVIEW,
        ReviewReason.VALIDATION_FAILED,
        2,
        None,
    )
    (review,) = harness.open_reviews()
    assert (review.id, review.status, review.reason, review.thread_id) == (
        result.review_id,
        ReviewStatus.OPEN,
        ReviewReason.VALIDATION_FAILED,
        f"receipt-{outcome.receipt_id}",
    )
    assert review_messages(harness, review.id) == _failures(second_reading)
    pending = harness.runner.pending_draft(outcome.receipt_id)
    assert pending is not None
    assert [line.line_total.amount_cents for line in pending.items] == [890, 658, 989]
    receipt = harness.receipt(outcome.receipt_id)
    assert (receipt.status, receipt.stage) == (
        ReceiptStatus.NEEDS_REVIEW,
        ProcessingStage.NEEDS_REVIEW,
    )


def test_ce_tc_025_a_single_attempt_goes_straight_to_review() -> None:
    harness = build_harness(
        ScriptedReader(inconsistent_answer()), settings=agent_settings(max_attempts=1)
    )

    result = graph(harness.process_image())

    assert scripted(harness).models == [EXTRACT_MODEL]
    assert (result.status, result.review_reason, result.attempts) == (
        ReceiptStatus.NEEDS_REVIEW,
        ReviewReason.VALIDATION_FAILED,
        1,
    )


# --- CE-TC-024: what counts as a failed attempt -------------------------------------------------


def _item_math() -> dict[str, Any]:
    value = answer()
    value["items"][0]["discount"] = "0.30"
    return value


def _weighed() -> dict[str, Any]:
    carrots = item(
        "LOOSE CARROTS", "1.54", quantity="0.460", unit="kg", unit_price="3.31", name="Carrots"
    )
    return receipt_answer([carrots], total="1.54")


def _blank_store() -> dict[str, Any]:
    return answer(store_name=" ")


def _no_date() -> dict[str, Any]:
    return answer(purchased_on=None)


def _unreadable() -> dict[str, Any]:
    value = answer()
    value["items"][0]["price_paid"] = "abc"
    return value


def _schema() -> dict[str, Any]:
    value = answer()
    value["items"][0]["price_paid"] = 8.99
    return value


def _discount_above_regular() -> dict[str, Any]:
    """R1: a saving larger than the item's regular price (8.99 - 9.99 = -1.00 paid)."""
    value = answer()
    value["items"][0]["discount"] = "9.99"
    value["items"][0]["price_paid"] = "-1.00"
    return value


def _two_failures() -> dict[str, Any]:
    value = _item_math()
    value["purchased_on"] = None
    return value


FAILED_ATTEMPTS = {
    "per-item": (_item_math, ["KS ORG EGGS 24CT", "8.99", "0.30"]),
    "discount-above-regular": (_discount_above_regular, ["KS ORG EGGS 24CT", "-1.00"]),
    "receipt-sum": (inconsistent_answer, ["25.46", "25.55"]),
    "weighed": (_weighed, ["LOOSE CARROTS", "1.54"]),
    "blank-store": (_blank_store, ["no store name was read"]),
    "unreadable-value": (_unreadable, ['"abc"', "price_paid"]),
    "schema": (_schema, ["price_paid"]),
    "two-failures": (_two_failures, ["KS ORG EGGS 24CT", "no purchase date was read"]),
}


@pytest.mark.parametrize(
    ("first", "in_feedback"), FAILED_ATTEMPTS.values(), ids=FAILED_ATTEMPTS.keys()
)
def test_ce_tc_024_each_kind_of_failed_attempt_is_retried_once_with_its_feedback(
    first: Any, in_feedback: list[str]
) -> None:
    harness = build_harness(ScriptedReader(first(), answer()))

    result = graph(harness.process_image())

    reader = scripted(harness)
    assert reader.models == [EXTRACT_MODEL, RETRY_MODEL]
    assert (result.status, result.attempts) == (ReceiptStatus.COMPLETED, 2)
    retry = reader.requests[1].text
    missing = [text for text in in_feedback if text not in retry]
    assert missing == [], retry
    assert reader.requests[0].text == _feedback_free_text()


def test_r1_a_discount_above_the_regular_price_twice_is_reviewed_never_failed() -> None:
    harness = build_harness(ScriptedReader(_discount_above_regular(), _discount_above_regular()))

    result = graph(harness.process_image())

    assert scripted(harness).models == [EXTRACT_MODEL, RETRY_MODEL]
    assert (result.status, result.review_reason) == (
        ReceiptStatus.NEEDS_REVIEW,
        ReviewReason.VALIDATION_FAILED,
    )
    pending = harness.runner.pending_draft(result.receipt_id)
    assert pending is not None
    assert [
        (line.kind, line.line_total.amount_cents, line.applies_to_position)
        for line in pending.items[:2]
    ] == [(LineKind.ITEM, 899, None), (LineKind.DISCOUNT, -999, None)]


@pytest.mark.parametrize(
    ("second_reply", "reason", "has_draft"),
    [
        (_schema(), ReviewReason.EXTRACTION_FAILED, False),
        (_unreadable(), ReviewReason.EXTRACTION_FAILED, False),
        (inconsistent_answer(), ReviewReason.VALIDATION_FAILED, True),
    ],
    ids=["schema", "unreadable-value", "inconsistent"],
)
def test_the_review_reason_says_whether_the_last_reading_parsed(
    second_reply: dict[str, Any], reason: ReviewReason, has_draft: bool
) -> None:
    harness = build_harness(ScriptedReader(_schema(), second_reply))

    result = graph(harness.process_image())

    assert (result.status, result.review_reason) == (ReceiptStatus.NEEDS_REVIEW, reason)
    assert (harness.runner.pending_draft(result.receipt_id) is not None) is has_draft


# --- CE-TC-026: a read that stops the run ---------------------------------------------------------

STOPS = {
    "refusal": ReaderRefusedError("the model declined to answer"),
    "max-tokens": ReaderTruncatedError("the answer was cut off at 16000 tokens"),
    "api-error": ReaderUnavailableError("the Claude API answered 529 overloaded"),
    "timeout": ReaderUnavailableError("the Claude API did not answer within 60 s"),
}


def _assert_stopped(harness: Harness, receipt_id: int, error: ReaderError) -> None:
    receipt = harness.receipt(receipt_id)
    assert (receipt.status, receipt.stage) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    assert (receipt.items, harness.product_ids(), harness.open_reviews()) == ((), [], ())
    errors = [e for e in harness.events(receipt_id) if e.kind is TraceKind.ERROR]
    assert [(e.name, str(error) in e.summary) for e in errors] == [(type(error).__name__, True)]
    saved_values(harness.checkpointer, receipt_id)


@pytest.mark.parametrize("error", STOPS.values(), ids=STOPS.keys())
def test_ce_tc_026_a_stopping_read_on_attempt_1_fails_the_receipt_with_its_checkpoint(
    error: ReaderError,
) -> None:
    harness = build_harness(ScriptedReader(error))

    with pytest.raises(ReaderError):
        harness.process_image()

    assert scripted(harness).models == [EXTRACT_MODEL]
    _assert_stopped(harness, 1, error)


@pytest.mark.parametrize("error", STOPS.values(), ids=STOPS.keys())
def test_ce_tc_026_a_stopping_read_on_attempt_2_keeps_attempt_1s_feedback(
    error: ReaderError,
) -> None:
    misread = _milk_misread()
    harness = build_harness(ScriptedReader(misread, error))

    with pytest.raises(ReaderError):
        harness.process_image()

    assert scripted(harness).models == [EXTRACT_MODEL, RETRY_MODEL]
    _assert_stopped(harness, 1, error)
    feedback = "\n".join(saved_values(harness.checkpointer, 1)["feedback"])
    assert [failure for failure in _failures(misread) if failure not in feedback] == []


# --- CE-TC-030: review on a Claude-read receipt ---------------------------------------------------


def test_ce_tc_030_a_person_resolves_the_review_with_corrected_values() -> None:
    harness = build_harness(
        ScriptedReader(inconsistent_answer()), settings=agent_settings(max_attempts=1)
    )
    parked = graph(harness.process_image())
    pending = harness.runner.pending_draft(parked.receipt_id)
    assert pending is not None
    sourdough = replace(
        pending.items[2], unit_price=Money(499, "CAD"), line_total=Money(998, "CAD")
    )
    draft = replace(pending, items=(*pending.items[:2], sourdough))

    resolved = harness.resolve(
        require_id(parked.review_id), draft, force=True, reason="checked the paper"
    )

    assert (resolved.status, resolved.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert [line.line_total.amount_cents for line in resolved.items] == [899, 658, 998]
    assert scripted(harness).calls == 1
    assert harness.node_runs(parked.receipt_id)[-3:] == ["persist", "match", "commit_matches"]


# --- CE-TC-032: card numbers in Claude's answer ---------------------------------------------------


def _with_card_numbers() -> dict[str, Any]:
    value = inconsistent_answer(store_name=f"{STORE_NAME} {TEST_PAN}", payment_last4="4242")
    value["items"][0]["printed_text"] = f"KS ORG EGGS {TEST_PAN}"
    value["items"][0]["name"] = f"Eggs {TEST_PAN}"
    value["adjustments"] = [adjustment("deposit", f"DEPOSIT {TEST_PAN}", "0.00")]
    return value


def test_ce_tc_032_card_numbers_in_the_answer_are_masked_everywhere_they_are_kept() -> None:
    """The tracer masks nothing (``RawTracer``): a card number in an event is a leak of the
    code that emitted it, ``TracingReader`` first."""
    harness = raw_traced(
        build_harness(ScriptedReader(_with_card_numbers()), settings=agent_settings(max_attempts=1))
    )

    result = graph(harness.process_image())

    assert result.status is ReceiptStatus.NEEDS_REVIEW
    checkpoint = json.dumps(saved_values(harness.checkpointer, result.receipt_id), default=str)
    assert card_numbers(checkpoint) == []
    assert card_numbers(repr(harness.review(require_id(result.review_id)))) == []
    for event in harness.events(result.receipt_id):
        assert card_numbers(f"{event.summary} {event.data!r}") == [], event.name
    pending = harness.runner.pending_draft(result.receipt_id)
    assert pending is not None
    assert card_numbers(repr(pending)) == []
    assert pending.payment_last4 == "4242"
    resolved = harness.resolve(require_id(result.review_id), force=True, reason="checked")
    assert card_numbers(repr(resolved)) == []
    assert resolved.payment_last4 == "4242"


def _schema_failure_quoting_a_card_number() -> dict[str, Any]:
    return answer(note=f"paid with {TEST_PAN}")


def test_r5_a_schema_failure_quoting_a_card_number_is_masked_everywhere_it_is_kept() -> None:
    harness = raw_traced(
        build_harness(
            ScriptedReader(
                _schema_failure_quoting_a_card_number(), _schema_failure_quoting_a_card_number()
            )
        )
    )

    result = graph(harness.process_image())

    assert (result.status, result.review_reason) == (
        ReceiptStatus.NEEDS_REVIEW,
        ReviewReason.EXTRACTION_FAILED,
    )
    retry = scripted(harness).requests[1].text
    assert ("note" in retry, card_numbers(retry)) == (True, [])
    checkpoint = json.dumps(saved_values(harness.checkpointer, result.receipt_id), default=str)
    assert card_numbers(checkpoint) == []
    assert card_numbers(repr(harness.review(require_id(result.review_id)))) == []
    leaks = [
        event.name
        for event in harness.events(result.receipt_id)
        if card_numbers(f"{event.summary} {event.data!r}")
    ]
    assert leaks == []


# --- R2: accepting a review unchanged keeps the reading's codes and sources -----------------------


def test_r2_accepting_a_review_unchanged_keeps_codes_and_department_sources() -> None:
    reading = metro_answer(total="42.14")
    reading["items"][2]["code"] = GTIN
    harness = build_harness(ScriptedReader(reading), settings=agent_settings(max_attempts=1))
    parked = graph(harness.process_image("metro"))
    assert parked.status is ReceiptStatus.NEEDS_REVIEW

    resolved = harness.resolve(require_id(parked.review_id), force=True, reason="checked")

    items = [line for line in resolved.items if line.kind is LineKind.ITEM]
    bananas, crackers = (harness.product(require_id(items[n].product_id)) for n in (0, 2))
    assert (bananas.category, bananas.field_sources.get("category")) == (
        Category.PRODUCE,
        FieldSource.RECEIPT_TEXT,
    )
    with harness.uow_factory(read_only=True) as uow:
        codes = [(row.scheme, row.code, row.product_id) for row in uow.product_codes.list()]
    assert codes == [(CodeScheme.GTIN, "0" + GTIN, crackers.id)]


# --- CE-TC-080: the prompt tells the model to copy, not to fix ------------------------------------


def test_ce_tc_080_every_read_says_copy_as_printed_and_never_adjust() -> None:
    misread = _milk_misread()
    harness = build_harness(ScriptedReader(misread, answer()))

    graph(harness.process_image())

    for call in (0, 1):
        prompt = _prompt(harness, call)
        assert (COPY_AS_PRINTED in prompt, NEVER_ADJUST in prompt) == (True, True), call
    assert _failures(misread)[0] in scripted(harness).requests[1].text


# --- tools around the reads ----------------------------------------------------------------------


def test_s5_ac_05_a_denied_tool_fails_the_run_loudly_without_retrying() -> None:
    """S5-AC-05 / AC-B-02 (in-process): a denial is traced, fails the run, is not retried."""
    harness = build_harness(
        ScriptedReader(answer()),
        wrap_tools=lambda gateway: DenyingToolGateway(gateway, "save_receipt"),
    )

    outcome = harness.process_image()

    assert outcome.graph is None
    assert outcome.failure is not None
    assert "denied: role=supervisor tool=save_receipt" in outcome.failure
    assert scripted(harness).calls == 1
    receipt = harness.receipt(outcome.receipt_id)
    assert (receipt.status, receipt.stage) == (ReceiptStatus.FAILED, ProcessingStage.FAILED)
    events = harness.events(outcome.receipt_id)
    denied = [e for e in events if e.kind is TraceKind.TOOL and e.name == "save_receipt"]
    assert [(e.data.get("allowed"), e.agent) for e in denied] == [(False, AgentRole.SUPERVISOR)]
    assert events[-1].kind is TraceKind.ERROR


def test_pc_tc_62_a_coded_receipts_tool_sequence(tmp_path: Path) -> None:
    """Barcode lookup PC-TC-62: each coded item is looked up by code, then (no product of ours)
    by a person's correction; the two PLU-named products are described from their entries."""
    plu = tmp_path / "plu.csv"
    plu.write_text(PLU_CSV, encoding="utf-8")
    harness = build_harness(
        ScriptedReader(coded_answer([C1, L3, L4])),
        settings=agent_settings(enrichment=True),
        catalog=PluCatalog(plu),
    )

    receipt_id = graph(harness.process_image()).receipt_id

    tools = [name for kind, name in harness.steps(receipt_id) if kind is TraceKind.TOOL]
    assert tools[tools.index("save_receipt") + 1 :] == [
        *["catalog_lookup", "product_lookup"] * 3,
        "save_matches",
        "describe_from_catalog",
        "describe_from_catalog",
    ]
    assert scripted(harness).calls == 1


def test_a_reading_missing_only_its_date_goes_to_review_without_a_retry() -> None:
    """The photo shows no date: a second reading cannot find one, so it costs nothing to skip
    (a missing date together with another failure is still retried: ``two-failures`` above)."""
    harness = build_harness(ScriptedReader(_no_date(), answer()))

    result = graph(harness.process_image())

    assert scripted(harness).models == [EXTRACT_MODEL]
    assert (result.status, result.review_reason) == (
        ReceiptStatus.NEEDS_REVIEW,
        ReviewReason.VALIDATION_FAILED,
    )
