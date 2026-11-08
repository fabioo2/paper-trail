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
  also a CATEGORY correction (subject: the alias, ``new_value`` the category, the store); an
  existing product's own category is left to enrichment (slice D).
- No embedding and no LLM call.
"""

import pytest

from paper_trail.domain.enums import AliasSource, Category, CorrectionKind, MatchSource
from paper_trail.domain.errors import DomainValidationError, NotFoundError
from paper_trail.domain.trace import TraceKind
from tests.agent_harness import Harness, build_harness
from tests.extractions import COSTCO_OCR, EGGS_ALIAS, MASKED_PAN, STORE_KEY, extraction
from tests.factories import TEST_PAN, product
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


def test_s4_ac_06_the_next_receipt_links_through_memory_without_vector_search() -> None:
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


def test_a_new_product_with_a_category() -> None:
    harness, receipt_id = _processed()
    old = require_id(harness.line(receipt_id, 0).product_id)

    corrected = harness.correct(
        receipt_id, 0, new_product_name="Kirkland Organic Eggs 24", category=Category.DAIRY_EGGS
    )

    created = harness.product(require_id(corrected.product_id))
    assert created.id not in (None, old)
    assert (created.canonical_name, created.category) == (
        "Kirkland Organic Eggs 24",
        Category.DAIRY_EGGS,
    )
    remembered = harness.latest_correction(EGGS_ALIAS, CorrectionKind.CATEGORY, STORE_KEY)
    assert remembered is not None
    assert remembered.new_value == "dairy_eggs"


def test_a_category_does_not_change_an_existing_product() -> None:
    harness, receipt_id = _processed()
    target = _other_product(harness)

    harness.correct(receipt_id, 0, product_id=target, category=Category.PANTRY)

    assert harness.product(target).category is Category.DAIRY_EGGS
    remembered = harness.latest_correction(EGGS_ALIAS, CorrectionKind.CATEGORY, STORE_KEY)
    assert remembered is not None
    assert remembered.new_value == "pantry"


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
