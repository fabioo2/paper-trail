"""VectorIndex contract (S1-AC-10, S1-AC-17): fake (brute-force cosine) and sqlite-vec.

Pinned (docs/tests/stage1.md): ``nearest`` with a wrong-length query also raises
DomainValidationError; deleting an unknown id is a no-op; equal distances order by product id.
"""

import math

import pytest

from paper_trail.domain.errors import DomainValidationError
from paper_trail.ports.search import VectorHit
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.storage import in_unit

DIMENSION = 768


def _axis(*weights: float) -> list[float]:
    """A 768-d vector whose first components are ``weights`` and the rest zero."""
    return [*weights, *([0.0] * (DIMENSION - len(weights)))]


def _upsert(uow_factory: UnitOfWorkFactory, product_id: int, vector: list[float]) -> None:
    in_unit(uow_factory, lambda uow: uow.vectors.upsert(product_id, vector))


def _nearest(uow_factory: UnitOfWorkFactory, vector: list[float], k: int) -> tuple[VectorHit, ...]:
    with uow_factory() as uow:
        return uow.vectors.nearest(vector, k=k)


def test_s1_ac_17_dimension_is_768(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-17: nomic-embed-text vectors have 768 components."""
    with uow_factory() as uow:
        assert uow.vectors.dimension == DIMENSION


@pytest.mark.parametrize("length", [0, 1, 767, 769, 1536])
def test_s1_ac_17_upsert_rejects_wrong_length(uow_factory: UnitOfWorkFactory, length: int) -> None:
    """S1-AC-17: upsert raises DomainValidationError when len(vector) != 768."""
    with pytest.raises(DomainValidationError):
        _upsert(uow_factory, 1, [0.5] * length)


def test_s1_ac_17_nearest_rejects_wrong_length(uow_factory: UnitOfWorkFactory) -> None:
    """Pinned: the query vector must have 768 components too."""
    with pytest.raises(DomainValidationError):
        _nearest(uow_factory, [1.0, 0.0], k=1)


def test_s1_ac_17_nearest_orders_by_ascending_cosine_distance(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """S1-AC-17: identical vector at distance ~0, then by increasing angle; at most k hits."""
    _upsert(uow_factory, 1, _axis(0.0, 1.0))  # 90 degrees from the query
    _upsert(uow_factory, 2, _axis(3.0, 0.0))  # same direction, different length
    _upsert(uow_factory, 3, _axis(1.0, 1.0))  # 45 degrees
    _upsert(uow_factory, 4, _axis(-1.0, 0.0))  # opposite

    hits = _nearest(uow_factory, _axis(1.0, 0.0), k=3)

    assert [hit.product_id for hit in hits] == [2, 3, 1]
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6)
    assert hits[1].distance == pytest.approx(1 - math.cos(math.pi / 4), abs=1e-6)
    assert hits[2].distance == pytest.approx(1.0, abs=1e-6)
    everything = _nearest(uow_factory, _axis(1.0, 0.0), k=10)
    assert [hit.product_id for hit in everything] == [2, 3, 1, 4]
    assert everything[-1].distance == pytest.approx(2.0, abs=1e-6)


def test_s1_ac_17_identical_vector_is_at_distance_zero(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-17: a dense vector finds itself at distance ~0."""
    vector = [math.sin(i) for i in range(DIMENSION)]
    _upsert(uow_factory, 7, vector)

    (hit,) = _nearest(uow_factory, vector, k=1)

    assert hit.product_id == 7
    assert hit.distance == pytest.approx(0.0, abs=1e-6)


def test_s1_ac_17_equal_distances_are_ordered_by_product_id(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Pinned: ties in distance are broken by ascending product id, so results are stable."""
    for product_id in (9, 3, 5):
        _upsert(uow_factory, product_id, _axis(2.0, 1.0))

    hits = _nearest(uow_factory, _axis(2.0, 1.0), k=3)

    assert [hit.product_id for hit in hits] == [3, 5, 9]


def test_s1_ac_17_upsert_twice_replaces_the_vector(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-17: the second upsert of a product replaces its vector (one row per product)."""
    _upsert(uow_factory, 1, _axis(1.0, 0.0))
    _upsert(uow_factory, 1, _axis(0.0, 1.0))

    hits = _nearest(uow_factory, _axis(0.0, 1.0), k=5)

    (hit,) = hits
    assert hit.product_id == 1
    assert hit.distance == pytest.approx(0.0, abs=1e-6)


def test_s1_ac_10_delete_removes_the_vector(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: deleted vectors are no longer returned; unknown ids are ignored."""
    _upsert(uow_factory, 1, _axis(1.0))
    _upsert(uow_factory, 2, _axis(1.0, 1.0))

    in_unit(uow_factory, lambda uow: uow.vectors.delete(1))
    in_unit(uow_factory, lambda uow: uow.vectors.delete(404))

    assert [hit.product_id for hit in _nearest(uow_factory, _axis(1.0), k=5)] == [2]


def test_s1_ac_10_nearest_on_an_empty_index(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: no vectors, no hits."""
    assert _nearest(uow_factory, _axis(1.0), k=3) == ()
