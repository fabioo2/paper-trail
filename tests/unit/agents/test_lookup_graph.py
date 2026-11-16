"""Lookups and guesses through the whole graph (claude-extraction plan D3, D4, V8; CE-TC-217,
229, 231, 233, 237, 239, 240, 247).

The harness's enricher has fake product databases and a second scripted Claude for the
guesses; the extraction reader answers the read only. ``enrich`` proposes, the supervisor's
``commit_enrichment`` looks up, guesses and writes, after ``commit_matches`` completed the
receipt, so no enrichment failure ever fails it.
"""

from typing import Any

from paper_trail.domain.enums import Category, FieldSource, ReceiptStatus
from paper_trail.domain.rules.item_codes import parse_code_field
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.lookups import LookupFailedError, LookupHit
from paper_trail.ports.reader import ReaderUnavailableError
from tests.agent_harness import Harness, agent_settings, build_harness
from tests.extractions import LOBLAWS_KEY, U1, coded_answer, metro_answer
from tests.fakes.lookups import FakeLookup
from tests.fakes.reader import ScriptedReader
from tests.support import require_id
from tests.unit.agents.graph_support import fresh_label

BARCODE_LINE = "4006381333931 TEST PENCILS"
GUESS: dict[str, Any] = {
    "name": "Guessed product",
    "brand": None,
    "size": None,
    "category": "pantry",
    "tags": ["guessed"],
}
HIT = LookupHit(name="Pencils", brand="Acme", quantity="12 each")


def _identity() -> str:
    code = parse_code_field(BARCODE_LINE.split(maxsplit=1)[0], LOBLAWS_KEY)
    assert code is not None
    assert code.identity is not None
    return code.identity


def _harness(lookup: FakeLookup, guesser: ScriptedReader, *answers: Any) -> Harness:
    return build_harness(
        ScriptedReader(*answers),
        settings=agent_settings(enrichment=True),
        lookups=[lookup],
        guesser=guesser,
    )


def _sources(harness: Harness, receipt_id: int) -> list[dict[str, FieldSource]]:
    return [
        dict(harness.product(require_id(line.product_id)).field_sources)
        for line in harness.receipt(receipt_id).items
    ]


def _notes(harness: Harness, receipt_id: int) -> list[str]:
    return [
        event.summary
        for event in harness.events(receipt_id)
        if event.kind is TraceKind.NOTE and event.name == "enrichment"
    ]


def test_a_barcode_is_looked_up_and_a_printed_line_guessed() -> None:
    off = FakeLookup("off", {_identity(): HIT})
    guesser = ScriptedReader(GUESS, GUESS)
    harness = _harness(off, guesser, coded_answer([BARCODE_LINE, U1]))

    outcome = harness.process_image(fresh_label())

    assert outcome.graph is not None
    assert outcome.graph.status is ReceiptStatus.COMPLETED
    assert off.asked == [_identity()]
    assert _sources(harness, outcome.receipt_id) == [
        {"canonical_name": FieldSource.OFF, "brand": FieldSource.OFF, "size": FieldSource.OFF,
         "category": FieldSource.LLM, "tags": FieldSource.LLM},
        dict.fromkeys(("canonical_name", "category", "tags"), FieldSource.LLM),
    ]  # fmt: skip
    assert [request.tag for request in guesser.requests] == ["enrichment/guess-v1"] * 2
    assert _notes(harness, outcome.receipt_id) == ["2 of 2 enriched"]


def test_failures_after_the_read_leave_the_receipt_completed_and_the_products_printed() -> None:
    off = FakeLookup("off", {_identity(): LookupFailedError("down")})
    guesser = ScriptedReader(then=ReaderUnavailableError("the Claude API cannot be reached"))
    harness = _harness(off, guesser, coded_answer([BARCODE_LINE, U1]))

    outcome = harness.process_image(fresh_label())

    assert outcome.failure is None
    assert harness.receipt(outcome.receipt_id).status is ReceiptStatus.COMPLETED
    assert harness.open_reviews() == ()
    assert (
        _sources(harness, outcome.receipt_id) == [{"canonical_name": FieldSource.RECEIPT_TEXT}] * 2
    )
    assert _notes(harness, outcome.receipt_id) == ["0 of 2 enriched"]


def test_enrich_proposes_and_a_resumed_run_writes_once() -> None:
    off = FakeLookup("off", {_identity(): HIT})
    guesser = ScriptedReader(GUESS)
    harness = _harness(off, guesser, coded_answer([BARCODE_LINE]))

    paused = harness.process_image(fresh_label(), interrupt_after="enrich")

    assert harness.receipt(paused.receipt_id).status is ReceiptStatus.COMPLETED
    assert _sources(harness, paused.receipt_id) == [{"canonical_name": FieldSource.RECEIPT_TEXT}]
    assert (off.asked, guesser.calls) == ([], 0)

    resumed = build_harness(
        ScriptedReader(),
        settings=harness.settings,
        clock=harness.clock,
        checkpointer=harness.checkpointer,
        uow_factory=harness.uow_factory,
        images=harness.images,
        lookups=[off],
        guesser=guesser,
    ).resume(paused.receipt_id)

    assert resumed.failure is None
    assert _sources(harness, paused.receipt_id)[0]["canonical_name"] is FieldSource.OFF
    assert (off.asked, guesser.calls) == ([_identity()], 1)


def test_the_department_is_told_to_claude_and_its_category_kept() -> None:
    guesser = ScriptedReader(then=GUESS)
    harness = _harness(FakeLookup("off"), guesser, metro_answer())

    outcome = harness.process_image(fresh_label())

    bananas = harness.product(require_id(harness.line(outcome.receipt_id, 0).product_id))
    assert '"department": "PRODUCE"' in guesser.requests[0].text
    assert (bananas.canonical_name, bananas.category) == ("Guessed product", Category.PRODUCE)
    assert bananas.field_sources["category"] is FieldSource.RECEIPT_TEXT
    assert _notes(harness, outcome.receipt_id) == ["7 of 7 enriched"]
