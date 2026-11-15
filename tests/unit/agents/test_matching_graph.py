"""Matching within a store by code and memory, through the whole graph (agent core slice C,
barcode-lookup plan PR 1a: R8, R11, A4, A10, owner answer on global codes).

In-process tools; the MCP transport is compared in ``test_transport_equivalence.py`` (S5-AC-04).

Pinned (docs/tests/barcode-lookup.md, test cases PC-TC-54..60):
- Graph: ``persist -> match -> commit_matches -> END``. ``match`` is the MATCHING agent and only
  reads (``catalog_lookup``, ``product_lookup``); ``commit_matches`` is the SUPERVISOR and writes
  everything with one ``save_matches`` call, also when no line is matched.
- Per ITEM line whose alias is not generic and that a review answer did not name: a line with a
  code (catalog on) -> ``catalog_lookup``; the code's product -> ``catalog``; else a person's
  PRODUCT_LINK correction for the line at the store (``product_lookup``, source ``memory``),
  the code attached with source ``correction``; else a reference entry -> a new product named
  from it; else a new product named ``printed_name(raw_text)``. A MATCHED alias never links a
  coded line. A line without a code (or any line with the catalog off) -> ``product_lookup``:
  a correction, then an alias at exactly that store, else a new printed-text product; no
  ``catalog_lookup``. Barcodes and PLUs name one product at every store; store item numbers
  and in-store codes belong to their store; no line is linked by text across stores.
- Zero model requests in ``match``: the scripted LLM holds the extraction replies only (any
  other request fails the run), no LLM event has agent MATCHING, nothing is embedded except a
  confirmed new product. ``product_similarity_search`` is never called.
"""

from dataclasses import dataclass
from typing import Any

import pytest

from paper_trail.application.process_receipt import ProcessOutcome
from paper_trail.domain.enums import (
    AgentRole,
    AliasSource,
    CodeScheme,
    CodeSource,
    CorrectionKind,
    FieldSource,
    MatchSource,
    ReceiptStatus,
)
from paper_trail.domain.models import JsonValue, ProductAlias, ProductCode
from paper_trail.domain.trace import TraceEvent, TraceKind
from paper_trail.ports.catalog import ProductCatalog
from paper_trail.ports.unit_of_work import UnitOfWork
from tests.agent_harness import Harness, agent_settings, build_harness
from tests.extractions import (
    C1,
    C2,
    COSTCO_OCR,
    KIRKLAND_EGGS,
    KIRKLAND_EGGS_ALIAS,
    L1,
    L3,
    L5,
    LOBLAWS,
    LOBLAWS_KEY,
    STORE_KEY,
    STORE_NAME,
    U1,
    WALMART,
    WALMART_KEY,
    coded_extraction,
    extraction,
    inconsistent_extraction,
    match_decision,
    mixed_lines_extraction,
    one_line_extraction,
)
from tests.factories import CREATED_AT, correction, product, receipt_draft
from tests.fakes.catalog import TEST_APPLE, FakeCatalog
from tests.fakes.embeddings import FakeEmbeddings, at_distance, basis
from tests.fakes.llm import Reply, ScriptedLlm
from tests.storage import in_unit
from tests.support import require_id

TOOL = TraceKind.TOOL
NEW, ALIAS, CATALOG, MEMORY = (
    MatchSource.NEW,
    MatchSource.ALIAS,
    MatchSource.CATALOG,
    MatchSource.MEMORY,
)
PRINTED_NAMES = ["KS ORG EGGS 24CT", "KS 2% MILK 4L", "KS SOURDOUGH"]
COSTCO_ALIASES = ["ks org eggs 24ct", "ks 2% milk 4l", "ks sourdough"]
L3_ALIAS = "tst apple"
READ_TOOLS = {"catalog_lookup", "product_lookup"}

type CodeRow = tuple[CodeScheme, str, str, int, CodeSource]


def _harness(
    *replies: Reply, catalog: ProductCatalog | None = None, catalog_on: bool = True
) -> Harness:
    """Only extraction replies: any other model request fails the run."""
    return build_harness(
        ScriptedLlm(*replies),
        settings=agent_settings(catalog=catalog_on),
        embeddings=FakeEmbeddings(),
        catalog=catalog,
    )


def _plu() -> FakeCatalog:
    return FakeCatalog({"3101": TEST_APPLE})


def _completed(outcome: ProcessOutcome) -> int:
    assert outcome.failure is None, outcome.failure
    assert outcome.graph is not None
    assert outcome.graph.status is ReceiptStatus.COMPLETED
    return outcome.receipt_id


def _run(harness: Harness, *raw_texts: str) -> int:
    return _completed(harness.process_text("\n".join(raw_texts), f"{len(raw_texts)}.txt"))


def _costco(harness: Harness, name: str = "costco.txt") -> int:
    return _completed(harness.process_text(COSTCO_OCR, name))


def _assert_no_model(harness: Harness, *receipt_ids: int) -> None:
    """PC-TC-58's recipe: one request per receipt (its extraction), none from matching."""
    assert harness.llm.calls == len(receipt_ids)
    for receipt_id in receipt_ids:
        assert not [
            event
            for event in harness.events(receipt_id)
            if event.kind is TraceKind.LLM
            and event.agent in {AgentRole.MATCHING, AgentRole.ENRICHMENT}
        ]
        assert (TOOL, "product_similarity_search") not in harness.steps(receipt_id)


def _matching_tools(harness: Harness, receipt_id: int) -> list[str]:
    return [
        event.name
        for event in harness.events(receipt_id)
        if event.kind is TOOL and event.agent is AgentRole.MATCHING
    ]


def _save_matches_event(harness: Harness, receipt_id: int) -> TraceEvent:
    saves = [e for e in harness.events(receipt_id) if e.kind is TOOL and e.name == "save_matches"]
    return saves[-1]


def _traced_sources(event: TraceEvent) -> dict[int, str]:
    output = event.data["output"]
    assert isinstance(output, dict)
    lines = output["lines"]
    assert isinstance(lines, list)
    return {_int(line, "position"): str(_field(line, "source")) for line in lines}


def _field(line: JsonValue, key: str) -> JsonValue:
    assert isinstance(line, dict)
    return line[key]


def _int(line: JsonValue, key: str) -> int:
    value = _field(line, key)
    assert isinstance(value, int)
    return value


def _linked(harness: Harness, receipt_id: int) -> list[tuple[int | None, MatchSource | None]]:
    return [(item.product_id, item.match_source) for item in harness.receipt(receipt_id).items]


def _seed_product(harness: Harness, name: str) -> int:
    return in_unit(
        harness.uow_factory,
        lambda uow: require_id(uow.products.add(product(canonical_name=name)).id),
    )


def _seed_code(harness: Harness, product_id: int, scheme: CodeScheme, code: str) -> None:
    """A person's code (``catalog add``); store_key "" (a barcode or PLU)."""
    value = ProductCode(
        None, product_id, scheme, "", code, CodeSource.MANUAL, CREATED_AT, CREATED_AT
    )
    in_unit(harness.uow_factory, lambda uow: uow.product_codes.upsert(value, replace=True))


def _seed_correction(harness: Harness, alias: str, store_key: str, product_id: int) -> None:
    value = correction(
        kind=CorrectionKind.PRODUCT_LINK,
        subject=alias,
        old_value=None,
        new_value=str(product_id),
        store_key=store_key,
    )
    in_unit(harness.uow_factory, lambda uow: uow.corrections.add(value))


def _seed_alias(
    harness: Harness,
    product_id: int,
    alias: str,
    store_key: str | None,
    source: AliasSource = AliasSource.MATCHED,
) -> None:
    value = ProductAlias(None, product_id, alias, store_key, source, CREATED_AT)
    in_unit(harness.uow_factory, lambda uow: uow.products.add_alias(value))


def _codes(harness: Harness) -> list[CodeRow]:
    def rows(uow: UnitOfWork) -> list[CodeRow]:
        return [
            (row.scheme, row.store_key, row.code, row.product_id, row.source)
            for row in uow.product_codes.list()
        ]

    return in_unit(harness.uow_factory, rows)


def _printed_text_product(harness: Harness, product_id: int, name: str) -> None:
    stored = harness.product(product_id)
    assert (
        stored.canonical_name,
        dict(stored.field_sources),
        stored.brand,
        stored.size_value,
        stored.size_unit,
        stored.category,
        stored.tags,
    ) == (name, {"canonical_name": FieldSource.RECEIPT_TEXT}, None, None, None, None, ())


# --- a first receipt: one printed-text product per line ----------------------------------------


def test_s4_ac_04_a_first_receipt_creates_a_product_per_line() -> None:
    """PC-TC-54 (e) / 56 for uncoded lines: named by their printed text, never embedded."""
    harness = _harness(extraction())

    receipt_id = _costco(harness)

    assert harness.match_sources(receipt_id) == [NEW, NEW, NEW]
    product_ids = [require_id(item.product_id) for item in harness.receipt(receipt_id).items]
    assert len(set(product_ids)) == 3
    for pid, name in zip(product_ids, PRINTED_NAMES, strict=True):
        _printed_text_product(harness, pid, name)
        assert not harness.has_vector(pid)
    for pid, alias in zip(product_ids, COSTCO_ALIASES, strict=True):
        assert harness.find_by_alias(alias, STORE_KEY) == harness.product(pid)
        (price,) = harness.price_history(pid)
        assert (price.receipt_id, price.store_key) == (receipt_id, STORE_KEY)
    assert harness.embeddings.calls == 0
    assert _codes(harness) == []
    assert harness.node_runs(receipt_id)[-3:] == ["persist", "match", "commit_matches"]
    tools = [name for kind, name in harness.steps(receipt_id) if kind is TOOL]
    assert tools[-4:] == ["product_lookup"] * 3 + ["save_matches"]
    _assert_no_model(harness, receipt_id)


# --- PC-TC-54: a coded line, in the order of A4 -------------------------------------------------


@dataclass(frozen=True)
class CodedCase:
    owned: bool  # the code names product "A"
    corrected: bool  # a person's PRODUCT_LINK correction names product "B"
    matched_alias: bool  # a MATCHED alias at the store names product "M"
    entry: bool  # the PLU catalog knows 3101
    product: str | None  # "A", "B", or None for a new product
    new_name: str | None
    source: MatchSource
    tools: tuple[str, ...]
    code: tuple[str, CodeSource]  # (the product the code names, its source)


LOOKUPS = ("catalog_lookup", "product_lookup")
CODED_CASES = {
    "a-code-is-ours": CodedCase(
        True, False, False, True, "A", None, CATALOG, ("catalog_lookup",), ("A", CodeSource.MANUAL)
    ),
    "b-correction": CodedCase(
        False, True, False, True, "B", None, MEMORY, LOOKUPS, ("B", CodeSource.CORRECTION)
    ),
    "c-code-before-correction": CodedCase(
        True, True, False, True, "A", None, CATALOG, ("catalog_lookup",), ("A", CodeSource.MANUAL)
    ),
    "d-plu-entry": CodedCase(
        False, False, False, True, None, "Test Apple", NEW, LOOKUPS, ("new", CodeSource.PLU)
    ),
    "e-nothing": CodedCase(
        False, False, False, False, None, "TST APPLE", NEW, LOOKUPS, ("new", CodeSource.RECEIPT)
    ),
    "f-matched-alias-is-not-a-correction": CodedCase(
        False, False, True, False, None, "TST APPLE", NEW, LOOKUPS, ("new", CodeSource.RECEIPT)
    ),
}  # fmt: skip


@pytest.mark.parametrize("case", CODED_CASES.values(), ids=CODED_CASES.keys())
def test_pc_tc_54_a_coded_line_follows_code_correction_entry_then_printed_text(
    case: CodedCase,
) -> None:
    harness = _harness(coded_extraction([L3]), catalog=_plu() if case.entry else FakeCatalog())
    seeded = {name: _seed_product(harness, name) for name in ("A", "B", "M")}
    if case.owned:
        _seed_code(harness, seeded["A"], CodeScheme.PLU, "3101")
    if case.corrected:
        _seed_correction(harness, L3_ALIAS, LOBLAWS_KEY, seeded["B"])
    if case.matched_alias:
        _seed_alias(harness, seeded["M"], L3_ALIAS, LOBLAWS_KEY)

    receipt_id = _run(harness, L3)

    ((linked, source),) = _linked(harness, receipt_id)
    if case.product is None:
        assert linked not in seeded.values()
        assert harness.product(require_id(linked)).canonical_name == case.new_name
        expected_owner = require_id(linked)
    else:
        assert linked == seeded[case.product]
        expected_owner = seeded[case.code[0]]
    assert source is case.source
    assert tuple(_matching_tools(harness, receipt_id)) == case.tools
    assert _codes(harness) == [(CodeScheme.PLU, "", "3101", expected_owner, case.code[1])]
    _assert_no_model(harness, receipt_id)


# --- PC-TC-56: an uncoded line: correction, then the store's alias, nothing else ---------------


UNCODED_CASES = {
    "correction-at-the-store": ("correction", STORE_KEY, MEMORY),
    "alias-at-the-store": ("alias", STORE_KEY, ALIAS),
    "alias-only-at-another-store": ("alias", WALMART_KEY, NEW),
    "global-alias-only": ("alias", None, NEW),
    "nothing": (None, None, NEW),
}


@pytest.mark.parametrize(
    ("memory", "store_key", "source"), UNCODED_CASES.values(), ids=UNCODED_CASES.keys()
)
def test_pc_tc_56_an_uncoded_line_uses_the_stores_memory_only(
    memory: str | None, store_key: str | None, source: MatchSource
) -> None:
    harness = _harness(one_line_extraction(U1))
    known = _seed_product(harness, "Known Eggs")
    if memory == "correction":
        _seed_correction(harness, COSTCO_ALIASES[0], STORE_KEY, known)
    elif memory == "alias":
        _seed_alias(harness, known, COSTCO_ALIASES[0], store_key)

    receipt_id = _run(harness, U1)

    ((linked, linked_source),) = _linked(harness, receipt_id)
    assert linked_source is source
    if source is NEW:
        assert linked != known
        _printed_text_product(harness, require_id(linked), U1)
    else:
        assert linked == known
    assert _matching_tools(harness, receipt_id) == ["product_lookup"]
    assert _codes(harness) == []
    _assert_no_model(harness, receipt_id)


def test_pc_tc_56_an_alias_under_the_old_tax_flags_is_still_found() -> None:
    """A8: ``tst apple mrj`` is how the line was normalized before ``mrj`` became a tax flag."""
    harness = _harness(one_line_extraction("TST APPLE MRJ", store_name=LOBLAWS))
    known = _seed_product(harness, "Test Apple")
    _seed_alias(harness, known, "tst apple mrj", LOBLAWS_KEY)

    receipt_id = _run(harness, "TST APPLE MRJ")

    assert _linked(harness, receipt_id) == [(known, ALIAS)]
    _assert_no_model(harness, receipt_id)


# --- PC-TC-57: lines that are never matched ------------------------------------------------------


def _coded_mixed_extraction() -> dict[str, Any]:
    """C1, then a deposit, a fee and a discount that print codes, a generic coded line and a
    generic uncoded one. 16.99 + 0.10 + 0.25 - 4.00 + 3.50 + 2.00 = 18.84."""

    def line(raw_text: str, kind: str, cents: int) -> dict[str, Any]:
        unit = cents if kind == "item" else None
        return {
            "raw_text": raw_text,
            "description": raw_text.title(),
            "quantity": "1",
            "unit_price_cents": unit,
            "line_total_cents": cents,
            "kind": kind,
        }

    value = one_line_extraction(C1, cents=1699)
    value["items"] += [
        line("1281 BOTTLE DEPOSIT", "deposit", 10),
        line("1234567 ECO FEE", "fee", 25),
        line("0000000001 / 1362394", "discount", -400),
        line("1543210 PRODUCE", "item", 350),
        line("PRODUCE", "item", 200),
    ]
    value["total_cents"] = 1884
    return value


def test_pc_tc_57_deposits_fees_discounts_and_generic_lines_are_never_matched() -> None:
    harness = _harness(_coded_mixed_extraction())

    receipt_id = _run(harness, "mixed")

    items = harness.receipt(receipt_id).items
    choc = require_id(items[0].product_id)
    assert _linked(harness, receipt_id) == [
        (choc, NEW),
        (None, None),
        (None, None),
        (None, None),
        (None, None),
        (None, None),
    ]
    assert _matching_tools(harness, receipt_id) == ["catalog_lookup", "product_lookup"]
    assert _codes(harness) == [(CodeScheme.STORE, STORE_KEY, "1362394", choc, CodeSource.RECEIPT)]
    assert harness.product_ids() == [choc]
    _assert_no_model(harness, receipt_id)


def test_a_receipt_with_nothing_to_match_still_commits_once() -> None:
    only_deposit: dict[str, Any] = mixed_lines_extraction()
    only_deposit["items"] = only_deposit["items"][1:2]
    only_deposit["total_cents"] = 10
    harness = _harness(only_deposit)

    receipt_id = _completed(harness.process_text("deposit only"))

    assert harness.steps(receipt_id)[-1] == (TOOL, "save_matches")
    event = _save_matches_event(harness, receipt_id)
    assert _traced_sources(event) == {}
    assert harness.product_ids() == []


# --- PC-TC-58: no model and no vectors in match, even where matching/v1 asked -------------------


def test_pc_tc_58_a_line_near_a_product_is_new_without_asking() -> None:
    """The state where ``matching/v1`` asked for a confirmation: a product at review distance
    and a confirmation reply queued. It stays unconsumed."""
    harness = build_harness(
        ScriptedLlm(
            one_line_extraction(KIRKLAND_EGGS, store_name=WALMART), match_decision("same", 1)
        ),
        embeddings=FakeEmbeddings({KIRKLAND_EGGS_ALIAS: basis(0)}),
    )

    def near(uow: UnitOfWork) -> int:
        product_id = require_id(uow.products.add(product(canonical_name="Near")).id)
        uow.vectors.upsert(product_id, at_distance(0.2))
        return product_id

    candidate = in_unit(harness.uow_factory, near)

    receipt_id = _run(harness, KIRKLAND_EGGS)

    ((linked, source),) = _linked(harness, receipt_id)
    assert (source, linked != candidate) == (NEW, True)
    assert harness.embeddings.calls == 0
    assert set(_matching_tools(harness, receipt_id)) <= READ_TOOLS
    _assert_no_model(harness, receipt_id)


# --- PC-TC-59: later receipts link by code ------------------------------------------------------


def test_pc_tc_59_store_codes_link_within_their_store_only() -> None:
    harness = _harness(
        coded_extraction([C1, C2], store_name=STORE_NAME),
        coded_extraction(
            ["1362394 CHOC PISTACHIO 12PK", "1543210 GOLD KIWI"], store_name=STORE_NAME
        ),
        coded_extraction(["1362394 CHOC PISTACH"]),
        coded_extraction([L5]),
        coded_extraction([L5], store_name=WALMART),
    )

    first = _run(harness, C1, C2)
    second = _run(harness, "1362394 CHOC PISTACHIO 12PK", "1543210 GOLD KIWI")
    loblaws = _run(harness, "1362394 CHOC PISTACH")
    in_store = _run(harness, L5)
    walmart = _run(harness, L5)

    choc, kiwi = (require_id(item.product_id) for item in harness.receipt(first).items)
    assert harness.match_sources(first) == [NEW, NEW]
    assert _linked(harness, second) == [(choc, CATALOG), (kiwi, CATALOG)]
    ((loblaws_choc, loblaws_source),) = _linked(harness, loblaws)
    ((chicken, _),) = _linked(harness, in_store)
    ((walmart_chicken, walmart_source),) = _linked(harness, walmart)
    assert (loblaws_source, walmart_source) == (NEW, NEW)
    assert len({choc, kiwi, require_id(loblaws_choc), chicken, walmart_chicken}) == 5
    assert _codes(harness) == [
        (CodeScheme.STORE, STORE_KEY, "1362394", choc, CodeSource.RECEIPT),
        (CodeScheme.STORE, STORE_KEY, "1543210", kiwi, CodeSource.RECEIPT),
        (CodeScheme.STORE, LOBLAWS_KEY, "1362394", require_id(loblaws_choc), CodeSource.RECEIPT),
        (CodeScheme.STORE, LOBLAWS_KEY, "2100580", chicken, CodeSource.RECEIPT),
        (CodeScheme.STORE, WALMART_KEY, "2100580", walmart_chicken, CodeSource.RECEIPT),
    ]
    _assert_no_model(harness, first, second, loblaws, in_store, walmart)


@pytest.mark.parametrize("raw_text", [L1, L3], ids=["gtin", "plu"])
def test_pc_tc_59_barcodes_and_plus_name_one_product_at_every_store(raw_text: str) -> None:
    harness = _harness(
        coded_extraction([raw_text]),
        coded_extraction([raw_text], store_name=WALMART, purchased_on="11/03/2026"),
        catalog=_plu(),
    )

    loblaws = _run(harness, raw_text)
    walmart = _run(harness, raw_text)

    ((shared, first_source),) = _linked(harness, loblaws)
    assert first_source is NEW
    assert _linked(harness, walmart) == [(shared, CATALOG)]
    prices = harness.price_history(require_id(shared))
    assert sorted((price.store_key, price.receipt_id) for price in prices) == sorted(
        [(LOBLAWS_KEY, loblaws), (WALMART_KEY, walmart)]
    )
    _assert_no_model(harness, loblaws, walmart)


# --- PC-TC-60: CATALOG__ENABLED=false ----------------------------------------------------------


def test_pc_tc_60_with_the_catalog_off_coded_lines_take_the_uncoded_path() -> None:
    harness = _harness(one_line_extraction(C1), one_line_extraction(C1), catalog_on=False)

    first = _run(harness, C1)
    second = _run(harness, C1)

    ((choc, source),) = _linked(harness, first)
    assert source is NEW
    _printed_text_product(harness, require_id(choc), "CHOC PISTACH")
    assert harness.find_by_alias("choc pistach", STORE_KEY) == harness.product(require_id(choc))
    assert _linked(harness, second) == [(choc, ALIAS)]
    for receipt_id in (first, second):
        assert "catalog_lookup" not in _matching_tools(harness, receipt_id)
    assert _codes(harness) == []
    _assert_no_model(harness, first, second)


# --- S4-AC-01 and AC-C-03: memory for uncoded lines ----------------------------------------------


def test_ac_c_03_new_then_alias_then_memory_after_a_correction() -> None:
    """Receipt A creates products; receipt B (same store, same text) hits the aliases; after
    `items correct`, reprocessing B finds the corrected product through correction memory, and
    the trace says so per line."""
    harness = _harness(extraction(), extraction(), extraction())
    first = _costco(harness, "a.txt")
    second = _completed(harness.process_image("b"))  # FakeOcr reads the same Costco text
    assert harness.match_sources(second) == [ALIAS, ALIAS, ALIAS]
    old_eggs = harness.line(first, 0).product_id

    corrected = harness.correct(second, 0, new_product_name="Kirkland Organic Eggs 24")
    again = harness.process_image("b", reprocess=True)

    assert _completed(again) == second
    assert again.duplicate is False
    items = harness.receipt(second).items
    assert (items[0].product_id, items[0].match_source) == (corrected.product_id, MEMORY)
    assert corrected.product_id != old_eggs
    assert harness.match_sources(second)[1:] == [ALIAS, ALIAS]
    assert harness.embeddings.calls == 0
    event = _save_matches_event(harness, second)
    assert _traced_sources(event) == {0: "memory", 1: "alias", 2: "alias"}
    assert "memory 1" in event.summary
    assert "alias 2" in event.summary
    assert harness.llm.calls == 3
    assert (TOOL, "product_similarity_search") not in harness.steps(second)


# --- slice C review note 2 (pinned in docs/tests/agent-core-D.md) ------------------------------


def test_review_note_2_a_line_a_person_linked_in_the_review_is_not_matched_again() -> None:
    """A review answer names the eggs' product (``DraftPayload.product_names``) and ``persist``
    links the line to it. ``match`` must leave that line alone: correction memory would point
    it elsewhere, and a person's answer outranks memory and aliases. The line gets no tool call
    and no decision; the other lines are matched as usual."""
    harness = build_harness(
        ScriptedLlm(extraction(), inconsistent_extraction()),
        settings=agent_settings(max_text_attempts=1),
    )
    first = _costco(harness, "a.txt")
    remembered = harness.correct(first, 0, new_product_name="Corrected Eggs")
    parked = harness.process_text(COSTCO_OCR, "b.txt")
    assert parked.graph is not None
    assert parked.graph.status is ReceiptStatus.NEEDS_REVIEW
    second = parked.receipt_id
    answer = receipt_draft(store_name=STORE_NAME, product_names={0: "Person's Eggs"})

    harness.resolve(require_id(parked.graph.review_id), answer)

    eggs, milk = harness.receipt(second).items
    linked = harness.product(require_id(eggs.product_id))
    assert linked.canonical_name == "Person's Eggs"
    assert eggs.product_id != remembered.product_id
    assert eggs.match_source is not MEMORY
    assert milk.match_source is ALIAS
    lookups = [e for e in harness.events(second) if e.kind is TOOL and e.name == "product_lookup"]
    assert len(lookups) == 1  # the milk only
    assert _traced_sources(_save_matches_event(harness, second)) == {1: "alias"}


# --- review round 1: lines as they are stored -----------------------------------------------

MASKED_GTIN_LINE = "644200050535 2 PK SOAP"  # a valid UPC-A; "644200050535 2" passes Luhn


def test_a_coded_line_that_masking_hides_is_matched_as_an_uncoded_line() -> None:
    """Fable P1: the line is stored masked, so it has no code: it takes the uncoded path (no
    code row, a MATCHED alias), and the next receipt finds it by that alias."""
    harness = _harness(coded_extraction([MASKED_GTIN_LINE]), coded_extraction([MASKED_GTIN_LINE]))

    first = _run(harness, MASKED_GTIN_LINE)
    second = _run(harness, MASKED_GTIN_LINE)

    (soap,) = harness.product_ids()
    _printed_text_product(harness, soap, "*********535 2 PK SOAP")
    assert _linked(harness, first) == [(soap, NEW)]
    assert _linked(harness, second) == [(soap, ALIAS)]
    assert _matching_tools(harness, first) == ["product_lookup"]
    assert _codes(harness) == []
    _assert_no_model(harness, first, second)


def test_a_store_code_on_a_receipt_without_a_store_is_matched_as_an_uncoded_line() -> None:
    """Fable P2: a store's own code names nothing without a store, so the line goes the uncoded
    way and no code row is written (the CHECK would refuse one)."""
    reply = coded_extraction([L5], store_name="")
    harness = _harness(reply, reply)
    parked = harness.process_text(L5)
    assert parked.graph is not None
    assert parked.graph.status is ReceiptStatus.NEEDS_REVIEW
    review_id = require_id(parked.graph.review_id)

    receipt_id = require_id(
        harness.resolve(review_id, force=True, reason="no store on the paper").id
    )

    assert harness.receipt(receipt_id).status is ReceiptStatus.COMPLETED
    (chicken,) = harness.product_ids()
    assert _linked(harness, receipt_id) == [(chicken, NEW)]
    assert "catalog_lookup" not in _matching_tools(harness, receipt_id)
    assert _codes(harness) == []
