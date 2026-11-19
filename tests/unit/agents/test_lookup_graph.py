"""Lookups and guesses through the whole graph (claude-extraction plan D3, D4, V8; CE-TC-217,
229, 231, 233, 237, 239, 240, 247).

The harness's enricher has fake product databases and a second scripted Claude for the
guesses; the extraction reader answers the read only. ``enrich`` proposes, the supervisor's
``commit_enrichment`` looks up, guesses and writes, after ``commit_matches`` completed the
receipt, so no enrichment failure ever fails it.
"""

from typing import Any

import pytest

from paper_trail.application.products import MergeProducts
from paper_trail.domain.enums import Category, FieldSource, MatchSource, ReceiptStatus
from paper_trail.domain.models import Product
from paper_trail.domain.rules.item_codes import parse_code_field
from paper_trail.domain.trace import TraceKind
from paper_trail.ports.lookups import LookupFailedError, LookupHit
from paper_trail.ports.reader import ReaderUnavailableError
from tests.agent_harness import Harness, agent_settings, build_harness
from tests.extractions import L3, LOBLAWS_KEY, U1, coded_answer, metro_answer
from tests.factories import product
from tests.fakes.catalog import TEST_APPLE, FakeCatalog
from tests.fakes.lookups import FakeLookup
from tests.fakes.reader import ScriptedReader
from tests.storage import in_unit
from tests.support import require_id
from tests.unit.agents.graph_support import fresh_label, scripted

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
    assert [request.tag for request in guesser.requests] == ["enrichment/guess-v2"] * 2
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


def _printed_product(name: str) -> Product:
    return product(
        canonical_name=name, brand=None, size_value=None, size_unit=None, category=None,
        tags=(), field_sources={"canonical_name": FieldSource.RECEIPT_TEXT},
    )  # fmt: skip


def _add_all(harness: Harness, items: list[Product]) -> list[int]:
    return in_unit(
        harness.uow_factory,
        lambda uow: [require_id(uow.products.add(item).id) for item in items],
    )


def _second_process(first: Harness, off: FakeLookup) -> Harness:
    return build_harness(
        ScriptedReader(),
        settings=first.settings,
        clock=first.clock,
        checkpointer=first.checkpointer,
        uow_factory=first.uow_factory,
        images=first.images,
        catalog=FakeCatalog({"3101": TEST_APPLE}),
        lookups=[off],
    )


def _first_process(off: FakeLookup, *answers: Any) -> Harness:
    return build_harness(
        ScriptedReader(*answers),
        settings=agent_settings(enrichment=True),
        catalog=FakeCatalog({"3101": TEST_APPLE}),
        lookups=[off],
    )


@pytest.mark.parametrize("interrupt_after", ["commit_matches", "enrich"])
def test_a_run_resumed_after_its_products_were_merged_away_describes_nothing(
    interrupt_after: str,
) -> None:
    """Cleanup D: the receipt's new products are merged into others and a person relinks the
    lines before the run resumes. The saved description and lookup named products that no
    longer exist, so they are skipped: no other product changes, and the receipt stays
    completed."""
    off = FakeLookup("off", {_identity(): HIT})
    first = _first_process(off, coded_answer([BARCODE_LINE, L3]))
    paused = first.process_image(fresh_label(), interrupt_after=interrupt_after)
    created = [require_id(line.product_id) for line in first.receipt(paused.receipt_id).items]
    kept = _add_all(first, [_printed_product("KEPT PENCILS"), _printed_product("KEPT APPLE")])
    for product_id, into_id in zip(created, kept, strict=True):
        MergeProducts(first.uow_factory)(product_id, into_id)
    relinked = _add_all(first, [_printed_product("BANANA"), _printed_product("BANANA TOO")])
    for position, product_id in enumerate(relinked):
        first.correct(paused.receipt_id, position, product_id=product_id)
    (added,) = _add_all(first, [_printed_product("NEWCOMER")])
    untouched = {pid: first.product(pid) for pid in (*kept, *relinked, added)}

    outcome = _second_process(first, off).resume(paused.receipt_id)

    assert outcome.failure is None
    assert first.receipt(paused.receipt_id).status is ReceiptStatus.COMPLETED
    assert {pid: first.product(pid) for pid in untouched} == untouched
    assert off.asked == []


@pytest.mark.parametrize("interrupt_after", ["commit_matches", "enrich"])
def test_a_run_resumed_after_a_person_relinked_its_lines_leaves_their_products_alone(
    interrupt_after: str,
) -> None:
    """Cleanup D: the products the run created still exist, so the description and the lookup
    go to them as decided, not to the products a person linked the lines to meanwhile."""
    off = FakeLookup("off", {_identity(): HIT})
    first = _first_process(off, coded_answer([BARCODE_LINE, L3]))
    paused = first.process_image(fresh_label(), interrupt_after=interrupt_after)
    created = [require_id(line.product_id) for line in first.receipt(paused.receipt_id).items]
    relinked = _add_all(first, [_printed_product("BANANA"), _printed_product("BANANA TOO")])
    for position, product_id in enumerate(relinked):
        first.correct(paused.receipt_id, position, product_id=product_id)
    stored = [first.product(product_id) for product_id in relinked]
    resumed = _second_process(first, off)

    outcome = resumed.resume(paused.receipt_id)

    assert outcome.failure is None
    assert [first.product(product_id) for product_id in relinked] == stored
    pencils = first.product(created[0])
    assert (pencils.brand, pencils.field_sources["brand"]) == ("Acme", FieldSource.OFF)
    described = [
        event.summary
        for event in resumed.events(paused.receipt_id)
        if event.kind is TraceKind.TOOL and event.name == "describe_from_catalog"
    ]
    assert len(described) == 1
    assert f"#{created[1]} " in described[0]


def test_a_run_paused_after_matching_links_a_merged_away_product_to_the_kept_one() -> None:
    """Cleanup D: the second receipt's decisions name the first receipt's products (by code and
    by alias); both are merged away before the decisions are saved. Saving decides those lines
    again without a model, through the moved code and alias, and links them to the kept
    products."""
    off = FakeLookup("off")
    answer = coded_answer([BARCODE_LINE, U1])
    first = _first_process(off, answer, answer)
    known = first.process_image(fresh_label())
    created = [require_id(line.product_id) for line in first.receipt(known.receipt_id).items]
    paused = first.process_image(fresh_label(), interrupt_after="match")
    kept = _add_all(first, [_printed_product("KEPT PENCILS"), _printed_product("KEPT EGGS")])
    for product_id, into_id in zip(created, kept, strict=True):
        MergeProducts(first.uow_factory)(product_id, into_id)
    resumed = _second_process(first, off)

    outcome = resumed.resume(paused.receipt_id)

    assert outcome.failure is None
    saved = first.receipt(paused.receipt_id)
    assert saved.status is ReceiptStatus.COMPLETED
    assert [(line.product_id, line.match_source) for line in saved.items] == [
        (kept[0], MatchSource.CATALOG),
        (kept[1], MatchSource.ALIAS),
    ]
    assert scripted(resumed).calls == 0
