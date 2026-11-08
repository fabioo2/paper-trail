"""Domain additions of agent core slice C (PLAN 3.4, PLAN 6, agent-core plan 8 delta 5).

Pinned (docs/tests/agent-core-C.md):
- ``ItemLink(position: int, product_id: int, match_source: MatchSource)`` in
  ``paper_trail.domain.models``, frozen with slots: one line linked to a product
  (``ReceiptRepository.link_items``).
- ``paper_trail.domain.rules.normalization``: ``GENERIC_ALIASES`` (a frozenset of normalized
  aliases that name no product, PLAN 6 risks: "PRODUCE", "DEPOSIT", ...) and
  ``is_generic_alias(alias) -> bool``, True for an empty alias or one in the stop-list. The
  matching agent never matches such a line, so it cannot pollute alias memory.
"""

import dataclasses

import pytest

from paper_trail.domain.enums import MatchSource
from paper_trail.domain.models import ItemLink
from paper_trail.domain.rules.normalization import (
    GENERIC_ALIASES,
    is_generic_alias,
    normalize_alias,
)


def test_item_link_is_a_frozen_value() -> None:
    link = ItemLink(position=2, product_id=7, match_source=MatchSource.VECTOR)

    assert (link.position, link.product_id, link.match_source) == (2, 7, MatchSource.VECTOR)
    assert link == ItemLink(2, 7, MatchSource.VECTOR)
    with pytest.raises(dataclasses.FrozenInstanceError):
        link.position = 3  # type: ignore[misc]


@pytest.mark.parametrize("printed", ["PRODUCE", "DEPOSIT", "Produce 3.50", "", "   "])
def test_generic_aliases_name_no_product(printed: str) -> None:
    assert is_generic_alias(normalize_alias(printed))


@pytest.mark.parametrize(
    "printed", ["KS ORG EGGS 24CT", "1234567 KS SOURDOUGH 4.99 H", "KIRKLAND ORGANIC EGGS 24"]
)
def test_product_aliases_are_not_generic(printed: str) -> None:
    assert not is_generic_alias(normalize_alias(printed))


def test_the_stop_list_is_normalized() -> None:
    """Every entry is its own normalized form, so a lookup by normalized alias finds it."""
    assert {"produce", "deposit"} <= GENERIC_ALIASES
    assert all(normalize_alias(alias) == alias for alias in GENERIC_ALIASES)
