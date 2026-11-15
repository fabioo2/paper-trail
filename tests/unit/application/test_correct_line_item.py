"""A person corrects the product of a receipt line, and it is remembered (S4-AC-06, PLAN 6).

Pinned (docs/tests/agent-core-C.md):
- ``CorrectLineItem(uow_factory, clock)`` in ``paper_trail.application.corrections``;
  ``__call__(receipt_id, position, *, product_id=None, new_product_name=None, category=None)
  -> LineItem`` (the corrected line). ``position`` is the line's stored 0-based position, as
  ``receipts show`` prints it.
- Exactly one of ``product_id`` and ``new_product_name`` (else DomainValidationError).
  NotFoundError for an unknown receipt, position or product. Nothing is written on an error.
- In one unit of work: a new product (the name masked like every free text, ``category`` as its
  category) or the given one; the line relinked with ``match_source`` MEMORY (a person's
  correction); its price observation moved to that product (none is created); a PRODUCT_LINK
  ``Correction`` (subject: the line's normalized alias, ``old_value`` the previous product id or
  None, ``new_value`` the new product id, the receipt's ``store_key``, ``receipt_id``,
  ``line_item_id``); a USER_CORRECTION alias (alias, store) for that product. With ``category``
  also a CATEGORY correction (subject: the alias, ``new_value`` the category, the store).
- No embedding and no LLM call.
- Product codes (barcode-lookup plan R10, TQ12; PC-TC-73..75): when the line's raw text parses
  to a code at the receipt's store, the same unit upserts it to the target with source
  ``correction`` (a person's: it replaces another product's mapping) and logs
  ``catalog.learned`` (scheme, store_key, code, product_id; no text). A new product is stamped
  ``canonical_name: person`` (and ``category: person`` with a category); with an existing
  product, a category is written to it with source ``person``.
"""

from typing import Any

import pytest
from structlog.testing import capture_logs

from paper_trail.domain.enums import (
    AliasSource,
    Category,
    CodeScheme,
    CodeSource,
    CorrectionKind,
    FieldSource,
    LineKind,
    MatchSource,
)
from paper_trail.domain.errors import DomainValidationError, NotFoundError
from paper_trail.domain.models import ProductCode, is_confirmed
from paper_trail.domain.trace import TraceKind
from tests.agent_harness import Harness, build_harness
from tests.extractions import (
    C1,
    COSTCO_OCR,
    EGGS_ALIAS,
    L1,
    L4,
    L5,
    LOBLAWS,
    LOBLAWS_KEY,
    MASKED_PAN,
    STORE_KEY,
    STORE_NAME,
    U1,
    coded_extraction,
    extraction,
)
from tests.factories import TEST_PAN, cad, line_item, product, receipt, simple_draft
from tests.fakes.llm import Reply, ScriptedLlm
from tests.storage import in_unit
from tests.support import require_id


def _processed(*replies: Reply) -> tuple[Harness, int]:
    harness = build_harness(ScriptedLlm(extraction(), *replies))
    outcome = harness.process_text(COSTCO_OCR, "a.txt")
    assert outcome.graph is not None
    return harness, outcome.receipt_id


def _other_product(harness: Harness) -> int:
    value = product(canonical_name="Organic Eggs (dozen x2)", category=Category.DAIRY_EGGS)
    return require_id(in_unit(harness.uow_factory, lambda uow: uow.products.add(value)).id)


def test_s4_ac_06_relinks_moves_the_price_and_remembers() -> None:
    harness, receipt_id = _processed()
    before = harness.line(receipt_id, 0)
    old = require_id(before.product_id)
    target = _other_product(harness)
    calls = (harness.llm.calls, harness.embeddings.calls)

    corrected = harness.correct(receipt_id, 0, product_id=target)

    assert corrected == harness.line(receipt_id, 0)
    assert (corrected.product_id, corrected.match_source) == (target, MatchSource.MEMORY)
    assert corrected.id == before.id
    assert harness.price_history(old) == ()
    (moved,) = harness.price_history(target)
    assert (moved.line_item_id, moved.receipt_id) == (before.id, receipt_id)
    memory = harness.latest_correction(EGGS_ALIAS, CorrectionKind.PRODUCT_LINK, STORE_KEY)
    assert memory is not None
    assert (memory.old_value, memory.new_value) == (str(old), str(target))
    assert (memory.receipt_id, memory.line_item_id) == (receipt_id, before.id)
    assert harness.find_by_alias(EGGS_ALIAS, STORE_KEY) == harness.product(target)
    sources = {(alias.alias, alias.store_key, alias.source) for alias in harness.aliases(target)}
    assert (EGGS_ALIAS, STORE_KEY, AliasSource.USER_CORRECTION) in sources
    assert (harness.llm.calls, harness.embeddings.calls) == calls
    assert harness.line(receipt_id, 1).match_source is MatchSource.NEW  # other lines untouched


def test_s4_ac_06_the_next_receipt_links_through_memory() -> None:
    harness, first = _processed(extraction())
    target = _other_product(harness)
    harness.correct(first, 0, product_id=target)
    embeddings_before = harness.embeddings.calls

    second = harness.process_text(COSTCO_OCR, "b.txt").receipt_id

    line = harness.line(second, 0)
    assert (line.product_id, line.match_source) == (target, MatchSource.MEMORY)
    assert harness.embeddings.calls == embeddings_before
    steps = harness.steps(second)
    assert (TraceKind.TOOL, "product_similarity_search") not in steps


@pytest.mark.parametrize(
    ("category", "sources"),
    [
        (
            Category.SNACKS_CANDY,
            {"canonical_name": FieldSource.PERSON, "category": FieldSource.PERSON},
        ),
        (None, {"canonical_name": FieldSource.PERSON}),
    ],
    ids=["with-category", "without-category"],
)
def test_pc_tc_75_a_new_product_is_named_by_a_person(
    category: Category | None, sources: dict[str, FieldSource]
) -> None:
    harness, receipt_id = _processed()
    old = require_id(harness.line(receipt_id, 0).product_id)

    corrected = harness.correct(
        receipt_id, 0, new_product_name="Choc Pistachio Bars", category=category
    )

    created = harness.product(require_id(corrected.product_id))
    assert created.id not in (None, old)
    assert (created.canonical_name, created.category, created.field_sources) == (
        "Choc Pistachio Bars",
        category,
        sources,
    )
    assert is_confirmed(created)
    remembered = harness.latest_correction(EGGS_ALIAS, CorrectionKind.CATEGORY, STORE_KEY)
    expected = None if category is None else category.value
    assert (None if remembered is None else remembered.new_value) == expected


def test_pc_tc_75_a_category_is_written_to_an_existing_product_as_a_persons() -> None:
    harness, receipt_id = _processed()
    value = product(
        canonical_name="Test Apple",
        category=Category.PRODUCE,
        field_sources={"canonical_name": FieldSource.PLU, "category": FieldSource.PLU},
    )
    target = require_id(in_unit(harness.uow_factory, lambda uow: uow.products.add(value)).id)

    harness.correct(receipt_id, 0, product_id=target, category=Category.DAIRY_EGGS)

    assert harness.product(target) == product(
        id=target,
        canonical_name="Test Apple",
        category=Category.DAIRY_EGGS,
        field_sources={"canonical_name": FieldSource.PLU, "category": FieldSource.PERSON},
    )
    remembered = harness.latest_correction(EGGS_ALIAS, CorrectionKind.CATEGORY, STORE_KEY)
    assert remembered is not None
    assert remembered.new_value == "dairy_eggs"


def test_a_new_products_name_is_masked() -> None:
    harness, receipt_id = _processed()

    corrected = harness.correct(receipt_id, 0, new_product_name=f"Eggs {TEST_PAN}")

    assert harness.product(require_id(corrected.product_id)).canonical_name == (
        f"Eggs {MASKED_PAN}"
    )


@pytest.mark.parametrize(
    ("product_id", "name"), [(None, None), (1, "both")], ids=["neither", "both"]
)
def test_exactly_one_target(product_id: int | None, name: str | None) -> None:
    harness, receipt_id = _processed()
    before = harness.receipt(receipt_id)

    with pytest.raises(DomainValidationError):
        harness.correct(receipt_id, 0, product_id=product_id, new_product_name=name)

    assert harness.receipt(receipt_id) == before


@pytest.mark.parametrize(
    ("receipt_offset", "position", "product_id"),
    [(1, 0, 1), (0, 9, 1), (0, 0, 999)],
    ids=["receipt", "position", "product"],
)
def test_unknown_things_are_not_found_and_nothing_changes(
    receipt_offset: int, position: int, product_id: int
) -> None:
    harness, receipt_id = _processed()
    before = harness.receipt(receipt_id)

    with pytest.raises(NotFoundError):
        harness.correct(receipt_id + receipt_offset, position, product_id=product_id)

    assert harness.receipt(receipt_id) == before
    assert harness.latest_correction(EGGS_ALIAS, CorrectionKind.PRODUCT_LINK, STORE_KEY) is None


def test_tc_63_a_corrected_discounted_line_keeps_its_discount() -> None:
    """TC-63 (R8, wiring of TC-83): the observation moved to the new product is still the one
    with discount -5.00 and paid 3.99."""
    harness = build_harness(ScriptedLlm())
    recorded = harness.record(simple_draft(product_names={0: "Eggs"})).receipt
    receipt_id = require_id(recorded.id)
    target = _other_product(harness)

    harness.correct(receipt_id, 0, product_id=target)

    (moved,) = harness.price_history(target)
    assert (moved.line_item_id, moved.discount, moved.paid, moved.on_sale) == (
        recorded.items[0].id,
        cad(-500),
        cad(399),
        True,
    )


# --- PC-TC-73, 74: correcting a coded line teaches its code ---------------------------------------


def _coded(raw_text: str, store_name: str, *more: dict[str, Any]) -> tuple[Harness, int]:
    harness = build_harness(ScriptedLlm(coded_extraction([raw_text], store_name=store_name), *more))
    outcome = harness.process_text(COSTCO_OCR, "a.txt")
    assert outcome.graph is not None
    return harness, outcome.receipt_id


def _codes(harness: Harness) -> list[tuple[CodeScheme, str, str, int, CodeSource]]:
    with harness.uow_factory(read_only=True) as uow:
        stored: tuple[ProductCode, ...] = uow.product_codes.list()
    return [(c.scheme, c.store_key, c.code, c.product_id, c.source) for c in stored]


@pytest.mark.parametrize(
    ("raw_text", "store_name", "learned"),
    [
        (C1, STORE_NAME, (CodeScheme.STORE, STORE_KEY, "1362394")),
        (L1, LOBLAWS, (CodeScheme.GTIN, "", "0060383064877")),
        (L4, LOBLAWS, (CodeScheme.PLU, "", "93101")),
        (L5, LOBLAWS, (CodeScheme.STORE, LOBLAWS_KEY, "2100580")),
        ("51234567890 COUPON", LOBLAWS, None),
        (U1, LOBLAWS, None),
    ],
    ids=["store-item", "gtin", "organic-plu", "in-store", "not-a-product-code", "no-code"],
)
def test_pc_tc_73_correcting_a_coded_line_teaches_the_code(
    raw_text: str, store_name: str, learned: tuple[CodeScheme, str, str] | None
) -> None:
    harness, receipt_id = _coded(raw_text, store_name)

    with capture_logs() as logs:
        corrected = harness.correct(receipt_id, 0, new_product_name="Named By A Person")

    target = require_id(corrected.product_id)
    events = [entry for entry in logs if entry["event"] == "catalog.learned"]
    if learned is None:
        assert _codes(harness) == []
        assert events == []
        return
    scheme, store_key, code = learned
    assert _codes(harness) == [(scheme, store_key, code, target, CodeSource.CORRECTION)]
    assert events == [
        {
            "event": "catalog.learned",
            "log_level": "info",
            "scheme": scheme,
            "store_key": store_key,
            "code": code,
            "product_id": target,
        }
    ]


def test_pc_tc_74_a_correction_replaces_the_mapping_and_the_next_receipt_follows() -> None:
    again = coded_extraction([C1], store_name=STORE_NAME)
    harness, first = _coded(C1, STORE_NAME, again, again)
    printed = require_id(harness.line(first, 0).product_id)
    assert _codes(harness) == [
        (CodeScheme.STORE, STORE_KEY, "1362394", printed, CodeSource.RECEIPT)
    ]

    named = require_id(harness.correct(first, 0, new_product_name="Choc Pistachio Bars").product_id)

    assert _codes(harness) == [
        (CodeScheme.STORE, STORE_KEY, "1362394", named, CodeSource.CORRECTION)
    ]
    second = harness.process_text(COSTCO_OCR, "b.txt").receipt_id
    assert (harness.line(second, 0).product_id, harness.match_sources(second)) == (
        named,
        [MatchSource.CATALOG],
    )

    harness.correct(first, 0, product_id=printed)

    assert _codes(harness) == [
        (CodeScheme.STORE, STORE_KEY, "1362394", printed, CodeSource.CORRECTION)
    ]
    third = harness.process_text(COSTCO_OCR, "c.txt").receipt_id
    assert (harness.line(third, 0).product_id, harness.match_sources(third)) == (
        printed,
        [MatchSource.CATALOG],
    )


def test_correcting_a_store_code_on_a_receipt_without_a_store_teaches_no_code() -> None:
    """Fable P2: a store's own code names nothing without a store; the correction is saved and
    no code row is written (the CHECK would refuse one and roll the correction back)."""
    harness = build_harness(ScriptedLlm())
    stored = receipt(store_name="", store_key="", items=(line_item(position=0, raw_text=L5),))
    receipt_id = require_id(in_unit(harness.uow_factory, lambda uow: uow.receipts.add(stored)).id)

    with capture_logs() as logs:
        corrected = harness.correct(receipt_id, 0, new_product_name="Chicken Breast")

    assert corrected.match_source is MatchSource.MEMORY
    assert _codes(harness) == []
    assert [entry for entry in logs if entry["event"] == "catalog.learned"] == []


def test_correcting_a_discount_line_teaches_no_code() -> None:
    """A discount's reference (``0000392208 / 1362394``) reads like a Costco code but names no
    product: only ITEM lines teach their code."""
    harness = build_harness(ScriptedLlm())
    discount = line_item(
        position=0,
        kind=LineKind.DISCOUNT,
        raw_text="0000392208 / 1362394",
        unit_price=None,
        line_total=cad(-400),
    )
    stored = receipt(items=(discount,))
    receipt_id = require_id(in_unit(harness.uow_factory, lambda uow: uow.receipts.add(stored)).id)

    harness.correct(receipt_id, 0, new_product_name="Not A Product")

    assert _codes(harness) == []
