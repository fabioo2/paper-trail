"""TextSearchIndex contract (S1-AC-16): the fake and the FTS5 adapter answer queries alike.

Trigger-level synchronisation (raw SQL updates and deletes, cascades) and bm25 scoring are
SQLite-only and live in ``tests/integration/test_fts.py``.

Pinned (docs/tests/stage1.md): the query is split into word tokens (maximal runs of Unicode
letters and digits); each token is a quoted prefix term and every term must match; a query
without tokens returns (); diacritics and case are ignored; hits are sorted by descending score.
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from paper_trail.domain.models import Receipt
from paper_trail.ports.search import HitKind, TextHit
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import line_item, product, receipt
from tests.storage import in_unit
from tests.support import require_id

# Inputs made only of FTS5 syntax (quotes, operators, column filters, grouping): no tokens that
# match the stored rows, so nothing is found, and above all nothing raises.
FTS_SYNTAX = ['"', "*", "NEAR(", "AND", "NOT", "-", ":", "(", ")", "^", '""', "'", "+", "{a b}:"]
FTS_SYNTAX += ["NEAR(eggs milk, 2)", "description:eggs AND milk", '"unterminated']


@pytest.fixture
def stored(uow_factory: UnitOfWorkFactory) -> Receipt:
    items = (
        line_item(position=0, raw_text="KS ORG EGGS 24CT",
                  description="Kirkland Organic Eggs 24 ct"),
        line_item(position=1, raw_text="CREME FRAICHE 250ML", description="Crème fraîche"),
        line_item(position=2, raw_text="BNLS CHKN THGH", description="Boneless chicken thighs",
                  quantity=Decimal("1.2")),
    )  # fmt: skip
    return in_unit(uow_factory, lambda uow: uow.receipts.add(receipt(items=items)))


def _search(uow_factory: UnitOfWorkFactory, query: str, limit: int = 20) -> tuple[TextHit, ...]:
    with uow_factory() as uow:
        return uow.text_search.search(query, limit=limit)


def _line_ids(uow_factory: UnitOfWorkFactory, query: str) -> list[int]:
    return [hit.id for hit in _search(uow_factory, query) if hit.kind is HitKind.LINE_ITEM]


def _product_ids(uow_factory: UnitOfWorkFactory, query: str) -> list[int]:
    return [hit.id for hit in _search(uow_factory, query) if hit.kind is HitKind.PRODUCT]


def test_s1_ac_16_finds_line_items_by_description_words(
    uow_factory: UnitOfWorkFactory, stored: Receipt
) -> None:
    """S1-AC-16: description words match, case-insensitively."""
    eggs, _, chicken = (require_id(item.id) for item in stored.items)

    assert _line_ids(uow_factory, "organic") == [eggs]
    assert _line_ids(uow_factory, "CHICKEN thighs") == [chicken]


def test_s1_ac_16_finds_line_items_by_raw_text_prefixes(
    uow_factory: UnitOfWorkFactory, stored: Receipt
) -> None:
    """S1-AC-16: "org egg" finds "KS ORG EGGS 24CT" (every token is a prefix term)."""
    eggs, _, chicken = (require_id(item.id) for item in stored.items)

    assert _line_ids(uow_factory, "org egg") == [eggs]
    assert _line_ids(uow_factory, "bnls chk") == [chicken]


def test_s1_ac_16_every_token_must_match(uow_factory: UnitOfWorkFactory, stored: Receipt) -> None:
    """Pinned: tokens are ANDed, so an unrelated extra word filters everything out."""
    assert _search(uow_factory, "eggs tractor") == ()


def test_s1_ac_16_diacritics_are_ignored(uow_factory: UnitOfWorkFactory, stored: Receipt) -> None:
    """S1-AC-16 / PLAN 3.5: remove_diacritics on both sides ("crème" and "fraiche" both match)."""
    creme = require_id(stored.items[1].id)

    assert _line_ids(uow_factory, "crème") == [creme]
    assert _line_ids(uow_factory, "fraiche") == [creme]


def test_s1_ac_16_finds_products_by_name_brand_and_tags(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-16: canonical_name, brand and tags are indexed for products."""
    eggs = require_id(in_unit(uow_factory, lambda uow: uow.products.add(product())).id)
    bananas_product = product(canonical_name="Bananas", brand=None, tags=("fair-trade", "fruit"))
    bananas = require_id(in_unit(uow_factory, lambda uow: uow.products.add(bananas_product)).id)

    assert _product_ids(uow_factory, "organic eggs") == [eggs]
    assert _product_ids(uow_factory, "kirkland signature") == [eggs]
    assert _product_ids(uow_factory, "free") == [eggs]  # tag "free-run"
    assert _product_ids(uow_factory, "fruit") == [bananas]
    assert _product_ids(uow_factory, "banana") == [bananas]


def test_s1_ac_16_hits_are_ordered_by_descending_score(
    uow_factory: UnitOfWorkFactory, stored: Receipt
) -> None:
    """S1-AC-16 / PLAN 3.4: line items and products in one list, higher score first."""
    in_unit(uow_factory, lambda uow: uow.products.add(product()))

    hits = _search(uow_factory, "eggs")

    assert {hit.kind for hit in hits} == {HitKind.LINE_ITEM, HitKind.PRODUCT}
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)
    assert all(isinstance(hit.score, float) for hit in hits)


def test_s1_ac_16_limit_caps_the_hits(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-16: at most `limit` hits."""
    items = tuple(line_item(position=i, raw_text=f"EGGS {i}") for i in range(5))
    in_unit(uow_factory, lambda uow: uow.receipts.add(receipt(items=items)))

    assert len(_search(uow_factory, "eggs", limit=3)) == 3
    assert len(_search(uow_factory, "eggs", limit=10)) == 5


def test_s1_ac_16_follows_receipt_changes_made_through_the_repository(
    uow_factory: UnitOfWorkFactory, stored: Receipt
) -> None:
    """S1-AC-16: replace_contents swaps the indexed text; the old words no longer match."""
    duck = line_item(raw_text="DUCK EGGS 6CT", description="Duck eggs")
    in_unit(uow_factory, lambda uow: uow.receipts.replace_contents(replace(stored, items=(duck,))))

    assert _search(uow_factory, "organic") == ()
    assert _search(uow_factory, "chicken") == ()
    assert len(_line_ids(uow_factory, "duck")) == 1


@pytest.mark.parametrize("query", FTS_SYNTAX)
def test_s1_ac_16_fts_syntax_never_raises(
    uow_factory: UnitOfWorkFactory, stored: Receipt, query: str
) -> None:
    """S1-AC-16: FTS5 syntax in user input is plain text: no error, and here nothing found."""
    assert _search(uow_factory, query) == ()


def test_s1_ac_16_operator_words_are_ordinary_prefixes(
    uow_factory: UnitOfWorkFactory, stored: Receipt
) -> None:
    """S1-AC-16: "OR" is not an operator but the prefix of "ORG" and "Organic"."""
    assert _line_ids(uow_factory, "OR") == [require_id(stored.items[0].id)]


@pytest.mark.parametrize("query", ["", "   ", "\t\n", "--", "***"])
def test_s1_ac_16_query_without_tokens_finds_nothing(
    uow_factory: UnitOfWorkFactory, stored: Receipt, query: str
) -> None:
    """Pinned: a query without tokens returns no hits."""
    assert _search(uow_factory, query) == ()


@pytest.mark.parametrize("query", ['"org', "egg*", "kirkland:", "-organic", "(eggs)", "eggs'"])
def test_s1_ac_16_syntax_around_words_is_stripped_not_interpreted(
    uow_factory: UnitOfWorkFactory, stored: Receipt, query: str
) -> None:
    """S1-AC-16: punctuation is dropped and the remaining words still search normally."""
    assert _line_ids(uow_factory, query) == [require_id(stored.items[0].id)]
