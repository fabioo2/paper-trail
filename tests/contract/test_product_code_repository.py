"""ProductCodeRepository contract, for the fake and the SQLite adapter (barcode-lookup R5;
PC-TC-24..28).

Pinned: ``find`` is exact on (scheme, store_key, code); ``for_product`` and ``list`` are in id
order; machine sources (``receipt``, ``plu``, ``off``, ``backfill``) never replace a row naming
another product and log one ``catalog.code_conflict`` warning with ids only; person sources
(``manual``, ``import``, ``adopted``, ``correction``, ``review``) replace it, keeping
``created_at``; ``remove`` reports False for an unknown code; codes join the unit of work.
"""

from dataclasses import replace
from datetime import timedelta

import pytest
from structlog.testing import capture_logs

from paper_trail.domain.enums import CodeScheme, CodeSource
from paper_trail.domain.models import ProductCode
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import product
from tests.fakes.clock import FakeClock
from tests.storage import in_unit
from tests.support import require_id

STORE = CodeScheme.STORE
GTIN = CodeScheme.GTIN
PLU = CodeScheme.PLU
MACHINE_SOURCES = [CodeSource.RECEIPT, CodeSource.PLU, CodeSource.OFF, CodeSource.BACKFILL]
PERSON_SOURCES = [
    CodeSource.MANUAL,
    CodeSource.IMPORT,
    CodeSource.ADOPTED,
    CodeSource.CORRECTION,
    CodeSource.REVIEW,
]
CONFLICT_KEYS = {
    "event",
    "log_level",
    "scheme",
    "store_key",
    "code",
    "existing_product_id",
    "refused_product_id",
}


def _products(uow_factory: UnitOfWorkFactory, count: int = 2) -> list[int]:
    return [
        require_id(in_unit(uow_factory, lambda uow: uow.products.add(product())).id)
        for _ in range(count)
    ]


def _code(  # noqa: PLR0913 - a test builder: keyword overrides with defaults
    product_id: int,
    clock: FakeClock,
    scheme: CodeScheme = STORE,
    store_key: str = "costco",
    code: str = "1362394",
    *,
    source: CodeSource = CodeSource.MANUAL,
) -> ProductCode:
    now = clock.now()
    return ProductCode(None, product_id, scheme, store_key, code, source, now, now)


def _upsert(uow_factory: UnitOfWorkFactory, code: ProductCode, *, replace: bool) -> ProductCode:
    return in_unit(uow_factory, lambda uow: uow.product_codes.upsert(code, replace=replace))


def _find(
    uow_factory: UnitOfWorkFactory, scheme: CodeScheme, store_key: str, code: str
) -> ProductCode | None:
    with uow_factory(read_only=True) as uow:
        return uow.product_codes.find(scheme, store_key, code)


def _all(uow_factory: UnitOfWorkFactory) -> tuple[ProductCode, ...]:
    with uow_factory(read_only=True) as uow:
        return uow.product_codes.list()


def test_pc_tc_24_upsert_then_find(uow_factory: UnitOfWorkFactory, fake_clock: FakeClock) -> None:
    a, _ = _products(uow_factory)
    code = _code(a, fake_clock)

    stored = _upsert(uow_factory, code, replace=False)

    assert stored == replace(code, id=stored.id)
    assert stored.id is not None
    assert _find(uow_factory, STORE, "costco", "1362394") == stored


@pytest.mark.parametrize(
    ("scheme", "store_key", "code"),
    [(STORE, "loblaws", "1362394"), (GTIN, "", "1362394"), (STORE, "costco", "01362394")],
    ids=["another store", "another scheme", "another code"],
)
def test_pc_tc_24_find_is_exact_on_all_three_parts(
    uow_factory: UnitOfWorkFactory,
    fake_clock: FakeClock,
    scheme: CodeScheme,
    store_key: str,
    code: str,
) -> None:
    a, _ = _products(uow_factory)
    _upsert(uow_factory, _code(a, fake_clock), replace=False)

    assert _find(uow_factory, scheme, store_key, code) is None


def test_pc_tc_25_for_product_and_list_filters(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    a, b = _products(uow_factory)
    seeded = [
        _upsert(uow_factory, code, replace=False)
        for code in (
            _code(a, fake_clock),
            _code(a, fake_clock, GTIN, "", "0060383064877"),
            _code(b, fake_clock, STORE, "loblaws", "2100580"),
            _code(b, fake_clock, PLU, "", "3101"),
        )
    ]

    with uow_factory(read_only=True) as uow:
        found = (
            uow.product_codes.for_product(a),
            uow.product_codes.list(),
            uow.product_codes.list(store_key="costco"),
            uow.product_codes.list(scheme=PLU),
            uow.product_codes.list(store_key="loblaws", scheme=STORE),
            uow.product_codes.list(store_key="walmart"),
        )

    assert found == (
        tuple(seeded[:2]),
        tuple(seeded),
        (seeded[0],),
        (seeded[3],),
        (seeded[2],),
        (),
    )


@pytest.mark.parametrize("source", MACHINE_SOURCES)
def test_pc_tc_26_machine_sources_never_replace_a_row(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock, source: CodeSource
) -> None:
    a, b = _products(uow_factory)
    existing = _upsert(uow_factory, _code(a, fake_clock), replace=False)
    fake_clock.advance(timedelta(minutes=5))

    with capture_logs() as logs:
        returned = _upsert(uow_factory, _code(b, fake_clock, source=source), replace=False)

    conflicts = [entry for entry in logs if entry["event"] == "catalog.code_conflict"]
    assert (returned, _all(uow_factory)) == (existing, (existing,))
    assert len(conflicts) == 1
    assert set(conflicts[0]) == CONFLICT_KEYS
    assert {key: conflicts[0][key] for key in CONFLICT_KEYS - {"event"}} == {
        "log_level": "warning",
        "scheme": "store",
        "store_key": "costco",
        "code": "1362394",
        "existing_product_id": a,
        "refused_product_id": b,
    }


def test_pc_tc_26_the_same_product_again_is_quiet(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    a, _ = _products(uow_factory)
    existing = _upsert(uow_factory, _code(a, fake_clock), replace=False)
    fake_clock.advance(timedelta(minutes=5))

    with capture_logs() as logs:
        _upsert(uow_factory, _code(a, fake_clock, source=CodeSource.RECEIPT), replace=False)

    assert (_all(uow_factory), logs) == ((existing,), [])


@pytest.mark.parametrize("source", PERSON_SOURCES)
def test_pc_tc_27_person_sources_replace_a_row(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock, source: CodeSource
) -> None:
    a, b = _products(uow_factory)
    existing = _upsert(uow_factory, _code(a, fake_clock, source=CodeSource.RECEIPT), replace=False)
    fake_clock.advance(timedelta(minutes=5))

    with capture_logs() as logs:
        returned = _upsert(uow_factory, _code(b, fake_clock, source=source), replace=True)

    expected = replace(existing, product_id=b, source=source, updated_at=fake_clock.now())
    assert (returned, _all(uow_factory)) == (expected, (expected,))
    assert [entry for entry in logs if entry["event"] == "catalog.code_conflict"] == []


def test_pc_tc_28_remove_keeps_the_product(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    a, _ = _products(uow_factory)
    _upsert(uow_factory, _code(a, fake_clock), replace=False)

    removed = in_unit(uow_factory, lambda uow: uow.product_codes.remove(STORE, "costco", "1362394"))
    unknown = in_unit(uow_factory, lambda uow: uow.product_codes.remove(STORE, "costco", "1543210"))

    with uow_factory(read_only=True) as uow:
        kept = uow.products.get(a)
    assert (removed, unknown, _find(uow_factory, STORE, "costco", "1362394")) == (
        True,
        False,
        None,
    )
    assert kept is not None


def test_pc_tc_28_a_rolled_back_upsert_leaves_no_row(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    a, _ = _products(uow_factory)

    with uow_factory() as uow:
        uow.product_codes.upsert(_code(a, fake_clock), replace=False)

    assert _all(uow_factory) == ()
