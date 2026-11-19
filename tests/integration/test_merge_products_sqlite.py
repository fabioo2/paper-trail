"""Merging a product into the same one (``MergeProducts``) on a real database file.

Words: S is the product merged away (deleted at the end), K the product kept, X a bystander.

Pinned: every line, price observation, code and alias of S becomes K's with nothing else
changed, PRODUCT_LINK corrections naming S (old or new value) name K, nothing references S
afterwards and X is untouched; matching finds K for S's printed text, alias and code; the lists,
purchases and price history count K's purchases from both stores once and agree; search follows
the fields K took and drops the ones it lost; S's id is never given again; a failure midway, a
second merge of a product already gone, two merges at once and a match naming S never leave a
half merge.
"""

import threading
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Self

import pytest
from sqlalchemy import Engine, text

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.store_catalog import StoreCatalog
from paper_trail.adapters.sqlite.unit_of_work import SqliteUnitOfWork
from paper_trail.application.catalog import CatalogTodo
from paper_trail.application.matching import MatchChoice, find_product, plan_matches, record_matches
from paper_trail.application.products import MergeProducts, ProductMerge
from paper_trail.application.summary import GetMonthSummary
from paper_trail.domain.enums import (
    AliasSource,
    Category,
    CodeScheme,
    CodeSource,
    CorrectionKind,
    FieldSource,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
)
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import Correction, Product, ProductCode, Receipt
from paper_trail.domain.rules.item_codes import parse_item_code
from paper_trail.domain.rules.normalization import normalize_alias, normalize_store_name
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.factories import CREATED_AT, correction, item_line, product, product_alias, receipt
from tests.fakes.clock import FakeClock
from tests.storage import StoredState, in_unit, seed_purchase, sqlite_uow_factory, stored_state
from tests.support import require_id

COSTCO = "COSTCO WHOLESALE #1234"
METRO = "METRO #123"
COSTCO_KEY = normalize_store_name(COSTCO)
METRO_KEY = normalize_store_name(METRO)
S_TEXT = "1362394 RED PEPPERS"
S_ALIAS = normalize_alias(S_TEXT)
S_CODE = "1362394"
K_CODE = "4455"
K_GTIN = "0062639300123"
GLOBAL_ALIAS = "red peppers 6pk"
CODED_ALIASED = "7654321 PEPPER MIX"
OLDEST, OLDER, NEWEST = date(2026, 10, 1), date(2026, 10, 15), date(2026, 11, 1)
TEXT, LLM, PERSON, OFF = (
    FieldSource.RECEIPT_TEXT,
    FieldSource.LLM,
    FieldSource.PERSON,
    FieldSource.OFF,
)


@dataclass(frozen=True, slots=True)
class Two:
    """F-TWO: S bought three times at Costco (one undated), K once at Metro, X beside K."""

    s: int
    k: int
    x: int
    costco: tuple[Receipt, ...]
    metro: Receipt


def _add(uow: UnitOfWork, item: Product) -> int:
    return require_id(uow.products.add(item).id)


def _code(product_id: int, scheme: CodeScheme, store_key: str, code: str) -> ProductCode:
    return ProductCode(
        None, product_id, scheme, store_key, code, CodeSource.RECEIPT, CREATED_AT, CREATED_AT
    )


def _link(subject: str, store_key: str, old: int | None, new: int) -> Correction:
    return correction(
        kind=CorrectionKind.PRODUCT_LINK,
        subject=subject,
        store_key=store_key,
        old_value=None if old is None else str(old),
        new_value=str(new),
    )


def _seed_two(factory: UnitOfWorkFactory) -> Two:
    """K and X first, so S has the highest id."""

    def products(uow: UnitOfWork) -> tuple[int, int, int]:
        k = _add(
            uow,
            product(
                canonical_name="Bell peppers",
                brand="Sunset",
                size_value=None,
                size_unit=None,
                category=None,
                tags=("organic",),
                field_sources={"canonical_name": PERSON, "brand": OFF, "tags": PERSON},
            ),
        )
        x = _add(uow, product(canonical_name="Butter", field_sources={"canonical_name": PERSON}))
        s = _add(
            uow,
            product(
                canonical_name="RED PEPPERS 6PK",
                brand=None,
                size_value=None,
                size_unit=None,
                category=Category.PRODUCE,
                tags=("red",),
                field_sources={"canonical_name": TEXT, "category": LLM, "tags": LLM},
            ),
        )
        return s, k, x

    s, k, x = in_unit(factory, products)
    costco = tuple(
        seed_purchase(
            factory,
            [item_line(0, S_TEXT, total, product_id=s, match_source=source)],
            store_name=COSTCO,
            purchased_on=day,
        )
        for day, total, source in [
            (OLDEST, 599, MatchSource.NEW),
            (OLDER, 649, MatchSource.CATALOG),
            (None, 579, MatchSource.CATALOG),
        ]
    )
    metro = seed_purchase(
        factory,
        [
            item_line(0, "BELL PEPPERS", 499, product_id=k, match_source=MatchSource.NEW),
            item_line(1, "BUTTER", 699, product_id=x, match_source=MatchSource.NEW),
        ],
        store_name=METRO,
        purchased_on=NEWEST,
    )

    def references(uow: UnitOfWork) -> None:
        for code in [
            _code(s, CodeScheme.STORE, COSTCO_KEY, S_CODE),
            _code(k, CodeScheme.STORE, METRO_KEY, K_CODE),
            _code(k, CodeScheme.GTIN, "", K_GTIN),
            _code(x, CodeScheme.STORE, COSTCO_KEY, "7777777"),
        ]:
            uow.product_codes.upsert(code, replace=True)
        for alias in [
            product_alias(s, alias=S_ALIAS, store_key=COSTCO_KEY, source=AliasSource.MATCHED),
            product_alias(s, alias=GLOBAL_ALIAS, store_key=None, source=AliasSource.MATCHED),
            product_alias(
                k, alias="bell peppers", store_key=METRO_KEY, source=AliasSource.USER_CORRECTION
            ),
            product_alias(x, alias="butter", store_key=METRO_KEY),
        ]:
            uow.products.add_alias(alias)
        for link in [
            _link(S_ALIAS, COSTCO_KEY, None, s),
            _link("other line", COSTCO_KEY, s, x),
            _link("bell peppers", METRO_KEY, s, k),
            correction(subject=S_ALIAS, store_key=COSTCO_KEY, new_value="produce"),
            correction(kind=CorrectionKind.FIELD, subject="receipt:1.total", new_value=str(s)),
            correction(subject="red peppers", old_value=str(s), new_value="produce"),
            _link("butter", METRO_KEY, None, x),
        ]:
            uow.corrections.add(link)

    in_unit(factory, references)
    return Two(s, k, x, costco, metro)


def _owned(state: StoredState, product_ids: set[int]) -> dict[str, set[int]]:
    """The ids of the lines, observations, codes and aliases of the products."""
    return {
        "lines": {
            require_id(item.id)
            for stored in state.receipts.values()
            for item in stored.items
            if item.product_id in product_ids
        },
        "prices": {i for i, row in state.prices.items() if row.product_id in product_ids},
        "codes": {i for i, row in state.product_codes.items() if row.product_id in product_ids},
        "aliases": {i for i, row in state.aliases.items() if row.product_id in product_ids},
    }


def _references(state: StoredState, product_id: int) -> list[object]:
    """Every row that names the product: lines, observations, codes, aliases and PRODUCT_LINK
    corrections (old or new value)."""
    named = str(product_id)
    return [
        *(
            item
            for stored in state.receipts.values()
            for item in stored.items
            if item.product_id == product_id
        ),
        *(row for row in state.prices.values() if row.product_id == product_id),
        *(row for row in state.product_codes.values() if row.product_id == product_id),
        *(row for row in state.aliases.values() if row.product_id == product_id),
        *(
            row
            for row in state.corrections.values()
            if row.kind is CorrectionKind.PRODUCT_LINK and named in (row.old_value, row.new_value)
        ),
    ]


@pytest.fixture
def factory(migrated_engine: Engine) -> UnitOfWorkFactory:
    return sqlite_uow_factory(migrated_engine, FakeClock())


@dataclass(frozen=True, slots=True)
class Merged:
    """F-TWO merged once (S into K): the rows before and after, and the merge's result."""

    two: Two
    factory: UnitOfWorkFactory
    engine: Engine
    before: StoredState
    after: StoredState
    merge: ProductMerge


@pytest.fixture(scope="module")
def merged(tmp_path_factory: pytest.TempPathFactory, migrated_template: Path) -> Iterator[Merged]:
    path = tmp_path_factory.mktemp("merged") / "merged.db"
    path.write_bytes(migrated_template.read_bytes())
    engine = create_engine(path)
    try:
        factory = sqlite_uow_factory(engine, FakeClock())
        two = _seed_two(factory)
        before = stored_state(engine)
        merge = MergeProducts(factory)(two.s, two.k)
        yield Merged(two, factory, engine, before, stored_state(engine), merge)
    finally:
        engine.dispose()


def test_every_reference_of_the_merged_product_moves_and_nothing_else_changes(
    merged: Merged,
) -> None:
    """D1-01..05, D1-07, D2-05: whole-table equality, so a reference left behind (unlinked by
    SET NULL, or gone by CASCADE) or any other change fails."""
    s, k = merged.two.s, merged.two.k
    before, after = merged.before, merged.after

    def moved(product_id: int) -> int:
        return k if product_id == s else product_id

    def renamed[T: str | None](value: T) -> T | str:
        return str(k) if value == str(s) else value

    assert after.receipts == {
        receipt_id: replace(
            stored,
            items=tuple(
                item
                if item.product_id is None
                else replace(item, product_id=moved(item.product_id))
                for item in stored.items
            ),
        )
        for receipt_id, stored in before.receipts.items()
    }
    assert after.prices == {
        row_id: replace(row, product_id=moved(row.product_id))
        for row_id, row in before.prices.items()
    }
    assert after.product_codes == {
        row_id: replace(row, product_id=moved(row.product_id))
        for row_id, row in before.product_codes.items()
    }
    assert after.aliases == {
        row_id: replace(row, product_id=moved(row.product_id))
        for row_id, row in before.aliases.items()
    }
    assert after.corrections == {
        row_id: replace(row, old_value=renamed(row.old_value), new_value=renamed(row.new_value))
        if row.kind is CorrectionKind.PRODUCT_LINK
        else row
        for row_id, row in before.corrections.items()
    }
    assert after.products == {
        **{pid: row for pid, row in before.products.items() if pid not in (s, k)},
        k: merged.merge.kept,
    }
    assert _references(after, s) == []
    assert _owned(after, {k}) == _owned(before, {s, k})
    with merged.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    assert (merged.merge.purchases, merged.merge.codes, merged.merge.aliases) == (3, 1, 2)


def test_matching_finds_the_kept_product_for_the_merged_ones_text_alias_and_code(
    merged: Merged,
) -> None:
    """D1-06: the correction, the global alias and the store code that named S name K."""
    k = merged.two.k
    with merged.factory(read_only=True) as uow:
        by_memory = find_product(uow, S_TEXT, COSTCO_KEY)
        by_alias = find_product(uow, GLOBAL_ALIAS, None)
    code = parse_item_code(S_TEXT, COSTCO_KEY)
    assert code is not None
    entry = StoreCatalog(merged.factory).lookup(code, COSTCO_KEY)

    assert by_memory is not None
    assert (by_memory[0].id, by_memory[1]) == (k, MatchSource.MEMORY)
    assert by_alias is not None
    assert (by_alias[0].id, by_alias[1]) == (k, MatchSource.ALIAS)
    assert entry is not None
    assert entry.product_id == k


def test_the_lists_count_the_kept_products_purchases_from_both_stores_once(
    merged: Merged,
) -> None:
    """D4-03, D4-04, D4-06: the list, the purchases and the price history agree."""
    s, k = merged.two.s, merged.two.k
    with merged.factory(read_only=True) as uow:
        summaries = {require_id(row.product.id): row for row in uow.products.summaries()}
        purchases = uow.products.purchases(k)
        history = uow.prices.history(k)

    assert s not in summaries
    listed = summaries[k]
    assert listed.times_bought == len(purchases) == 4
    assert [(row.store_name, row.purchased_on, row.paid.amount_cents) for row in purchases] == [
        (METRO, NEWEST, 499),
        (COSTCO, OLDER, 649),
        (COSTCO, OLDEST, 599),
        (COSTCO, None, 579),
    ]
    assert listed.last_purchase.purchased_on == NEWEST
    assert [(p.store_name, p.purchased_on, p.paid) for p in listed.recent] == [
        (row.store_name, row.purchased_on, row.paid)
        for row in purchases
        if row.purchased_on is not None
    ]
    assert len(history) == 3
    assert sum(row.paid.amount_cents for row in purchases) == 599 + 649 + 579 + 499


@pytest.fixture
def two(factory: UnitOfWorkFactory) -> Two:
    return _seed_two(factory)


def test_the_merged_products_id_is_never_given_again(
    factory: UnitOfWorkFactory, migrated_engine: Engine, two: Two
) -> None:
    """D1-08: S had the highest id; the next product gets a new one (migration 0011), so
    nothing that still names S's id names it."""
    MergeProducts(factory)(two.s, two.k)

    added = in_unit(factory, lambda uow: _add(uow, product(canonical_name="Paper towels")))

    assert added == two.s + 1
    assert _references(stored_state(migrated_engine), two.s) == []
    with factory(read_only=True) as uow:
        found = find_product(uow, S_TEXT, COSTCO_KEY)
    assert found is not None
    assert found[0].id == two.k


def test_search_follows_the_fields_the_kept_product_took_and_lost(
    factory: UnitOfWorkFactory,
) -> None:
    """D4-01, D4-02: K loses its name, brand and tag to S's better-sourced ones; the old words
    stop finding it, S's words find K only, also after a new product is added."""

    def seed(uow: UnitOfWork) -> tuple[int, int]:
        k = _add(
            uow,
            product(
                canonical_name="Capsicum Oldname",
                brand="Oldbrand",
                tags=("oldtag",),
                field_sources={"canonical_name": LLM, "brand": LLM, "tags": LLM},
            ),
        )
        s = _add(
            uow,
            product(
                canonical_name="Pepper Newname",
                brand="Sunset",
                tags=("newtag",),
                field_sources={"canonical_name": PERSON, "brand": OFF, "tags": PERSON},
            ),
        )
        return s, k

    s, k = in_unit(factory, seed)

    MergeProducts(factory)(s, k)
    added = in_unit(factory, lambda uow: _add(uow, product(canonical_name="Unrelated Thing")))

    with factory(read_only=True) as uow:
        found = {
            word: uow.text_search.product_ids(word)
            for word in [
                "Oldname",
                "Oldbrand",
                "oldtag",
                "Newname",
                "Sunset",
                "newtag",
                "Unrelated",
            ]
        }
    assert found == {
        "Oldname": (),
        "Oldbrand": (),
        "oldtag": (),
        "Newname": (k,),
        "Sunset": (k,),
        "newtag": (k,),
        "Unrelated": (added,),
    }


def test_the_same_code_and_alias_in_other_scopes_all_survive_unchanged(
    factory: UnitOfWorkFactory, migrated_engine: Engine
) -> None:
    """D2-01, D2-02, D2-04: one code's digits at two stores, one alias text at two stores and
    globally; the moved rows keep their source and dates."""

    def seed(uow: UnitOfWork) -> tuple[int, int]:
        s = _add(uow, product(canonical_name="S"))
        k = _add(uow, product(canonical_name="K"))
        uow.product_codes.upsert(_code(s, CodeScheme.STORE, COSTCO_KEY, "1234567"), replace=True)
        uow.product_codes.upsert(_code(k, CodeScheme.STORE, METRO_KEY, "1234567"), replace=True)
        uow.products.add_alias(
            product_alias(s, alias="red peppers", store_key=COSTCO_KEY, source=AliasSource.MATCHED)
        )
        uow.products.add_alias(
            product_alias(
                k, alias="red peppers", store_key=METRO_KEY, source=AliasSource.USER_CORRECTION
            )
        )
        uow.products.add_alias(product_alias(k, alias="red peppers", store_key=None))
        return s, k

    s, k = in_unit(factory, seed)
    before = stored_state(migrated_engine)

    MergeProducts(factory)(s, k)

    after = stored_state(migrated_engine)
    assert after.product_codes == {
        row_id: replace(row, product_id=k) for row_id, row in before.product_codes.items()
    }
    assert after.aliases == {
        row_id: replace(row, product_id=k) for row_id, row in before.aliases.items()
    }
    with factory(read_only=True) as uow:
        for store_key in (COSTCO_KEY, METRO_KEY, None):
            alias = uow.products.alias_at("red peppers", store_key)
            assert alias is not None
            assert alias.product_id == k
        for store_key in (COSTCO_KEY, METRO_KEY):
            code = uow.product_codes.find(CodeScheme.STORE, store_key, "1234567")
            assert code is not None
            assert code.product_id == k


def test_one_receipt_that_bought_both_lists_both_lines_on_the_kept_product(
    factory: UnitOfWorkFactory,
) -> None:
    """D2-03."""

    def seed(uow: UnitOfWork) -> tuple[int, int]:
        return _add(uow, product(canonical_name="S")), _add(uow, product(canonical_name="K"))

    s, k = in_unit(factory, seed)
    stored = seed_purchase(
        factory,
        [
            item_line(0, "PEPPERS", 300, product_id=s, match_source=MatchSource.NEW),
            item_line(1, "PEPPERS RED", 400, product_id=k, match_source=MatchSource.NEW),
        ],
    )

    MergeProducts(factory)(s, k)

    with factory(read_only=True) as uow:
        purchases = uow.products.purchases(k)
        history = uow.prices.history(k)
        (summary,) = uow.products.summaries()
    assert [(row.receipt_id, row.paid.amount_cents) for row in purchases] == [
        (stored.id, 400),
        (stored.id, 300),
    ]
    assert sorted(row.line_item_id for row in history) == [
        require_id(item.id) for item in stored.items
    ]
    assert summary.times_bought == 2


def test_a_product_with_nothing_attached_merges_cleanly(
    factory: UnitOfWorkFactory, migrated_engine: Engine, two: Two
) -> None:
    """D1-09: a never-bought product with no codes, aliases or corrections."""
    s = in_unit(factory, lambda uow: _add(uow, product(canonical_name="Never bought")))
    k = two.s
    before = stored_state(migrated_engine)

    MergeProducts(factory)(s, k)

    after = stored_state(migrated_engine)
    assert s not in after.products
    assert _references(after, s) == []
    assert _owned(after, {k}) == _owned(before, {s, k})


@pytest.mark.parametrize("confirmed", [True, False], ids=["kept-confirmed", "both-unconfirmed"])
def test_the_to_check_count_follows_the_merge(factory: UnitOfWorkFactory, confirmed: bool) -> None:
    """D4-05: S is unconfirmed; one product fewer to check, K listed once with the earliest
    line of both as its printed text, and the home count agrees with the list."""

    def seed(uow: UnitOfWork) -> tuple[int, int]:
        s = _add(uow, product(canonical_name="S", field_sources={"canonical_name": TEXT}))
        k_source = PERSON if confirmed else LLM
        k = _add(uow, product(canonical_name="K", field_sources={"canonical_name": k_source}))
        return s, k

    s, k = in_unit(factory, seed)
    for day, printed, product_id in [(OLDEST, "S EARLY", s), (NEWEST, "K LATE", k)]:
        seed_purchase(
            factory,
            [item_line(0, printed, 100, product_id=product_id, match_source=MatchSource.NEW)],
            purchased_on=day,
        )
    with factory(read_only=True) as uow:
        count_before = uow.products.unconfirmed_count()

    MergeProducts(factory)(s, k)

    todo = CatalogTodo(factory)()
    with factory(read_only=True) as uow:
        assert uow.products.unconfirmed_count() == count_before - 1
    assert [(row.product_id, row.raw_text) for row in todo] == (
        [] if confirmed else [(k, "S EARLY")]
    )
    home = GetMonthSummary(factory, FakeClock())(NEWEST)
    assert home.unconfirmed_products == len(todo)


class MergeStepError(RuntimeError):
    """A repository failed midway through the merge."""


class _Failing:
    def __init__(self, inner: object, method: str) -> None:
        self._inner = inner
        self._method = method

    def __getattr__(self, name: str) -> object:
        if name == self._method:

            def fail(*_: object) -> None:
                msg = f"{name} failed"
                raise MergeStepError(msg)

            return fail
        return getattr(self._inner, name)


def _failing_factory(engine: Engine, repository: str, method: str) -> UnitOfWorkFactory:
    class Unit(SqliteUnitOfWork):
        def __enter__(self) -> Self:
            unit = super().__enter__()
            setattr(unit, repository, _Failing(getattr(unit, repository), method))
            return unit

    def factory(*, read_only: bool = False) -> SqliteUnitOfWork:
        return Unit(engine, FakeClock(), read_only=read_only)

    return factory


def _search_snapshot(factory: UnitOfWorkFactory) -> dict[str, tuple[int, ...]]:
    with factory(read_only=True) as uow:
        return {word: uow.text_search.product_ids(word) for word in ["RED", "Bell", "Sunset"]}


@pytest.mark.parametrize(
    ("repository", "method"),
    [("corrections", "rename_product"), ("products", "delete")],
)
def test_a_failure_midway_leaves_both_products_untouched(
    factory: UnitOfWorkFactory, migrated_engine: Engine, two: Two, repository: str, method: str
) -> None:
    """D5-03."""
    before = stored_state(migrated_engine)
    search = _search_snapshot(factory)

    with pytest.raises(MergeStepError):
        MergeProducts(_failing_factory(migrated_engine, repository, method))(two.s, two.k)

    assert stored_state(migrated_engine) == before
    assert _search_snapshot(factory) == search


def _merge(factory: UnitOfWorkFactory, product_id: int, into_id: int) -> None:
    MergeProducts(factory)(product_id, into_id)


@pytest.mark.parametrize(
    "case",
    ["back-again", "kept-merged-elsewhere", "chain"],
)
def test_merges_one_after_another(
    factory: UnitOfWorkFactory, migrated_engine: Engine, two: Two, case: str
) -> None:
    """D5-04, D5-06, D5-07: a product already merged away is not found and nothing changes;
    a chain ends with everything on the last product."""
    s, k, x = two.s, two.k, two.x
    before = stored_state(migrated_engine)
    first, second, refused = {
        "back-again": ((s, k), (k, s), True),
        "kept-merged-elsewhere": ((k, x), (s, k), True),
        "chain": ((s, k), (k, x), False),
    }[case]
    _merge(factory, *first)
    between = stored_state(migrated_engine)

    if refused:
        with pytest.raises(NotFoundError):
            _merge(factory, *second)
        assert stored_state(migrated_engine) == between
        return
    _merge(factory, *second)
    after = stored_state(migrated_engine)
    assert _references(after, s) == _references(after, k) == []
    assert _owned(after, {x}) == _owned(before, {s, k, x})


def test_two_merges_of_the_pair_at_once_end_with_one_product_holding_everything(
    migrated_engine: Engine, two: Two
) -> None:
    """D5-05: S into K and K into S at the same moment, each on its own connection."""
    factory = sqlite_uow_factory(migrated_engine, FakeClock())
    before = stored_state(migrated_engine)
    outcomes: list[str] = []
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def merge(product_id: int, into_id: int) -> None:
        start.wait()
        try:
            _merge(factory, product_id, into_id)
            outcomes.append("merged")
        except NotFoundError:
            outcomes.append("not found")
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=merge, args=pair) for pair in [(two.s, two.k), (two.k, two.s)]
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert sorted(outcomes) == ["merged", "not found"]
    after = stored_state(migrated_engine)
    (left,) = [pid for pid in (two.s, two.k) if pid in after.products]
    (gone,) = {two.s, two.k} - {left}
    assert _owned(after, {left}) == _owned(before, {two.s, two.k})
    assert _references(after, gone) == []


@pytest.mark.parametrize(
    ("printed", "linked"),
    [(S_TEXT, MatchSource.MEMORY), ("NOTHING KNOWN", None), (CODED_ALIASED, None)],
    ids=["remembered", "unknown", "coded-by-alias-only"],
)
def test_a_match_that_chose_the_merged_product_is_decided_again(
    factory: UnitOfWorkFactory,
    migrated_engine: Engine,
    two: Two,
    printed: str,
    linked: MatchSource | None,
) -> None:
    """D5-09: the matching run decided S before the merge and saves after it. The line is
    decided again without a model: S's correction now names K, so it links to K; a line nothing
    names is refused with nothing saved, never left unlinked, and so is a coded line only an
    alias names (a shared text never links a coded line)."""
    pending = in_unit(
        factory,
        lambda uow: uow.receipts.add(
            receipt(
                status=ReceiptStatus.PROCESSING,
                stage=ProcessingStage.MATCHING,
                items=(item_line(0, printed, 599),),
            )
        ),
    )
    receipt_id = require_id(pending.id)
    MergeProducts(factory)(two.s, two.k)
    alias = product_alias(two.k, alias=normalize_alias(CODED_ALIASED), store_key=COSTCO_KEY)
    in_unit(factory, lambda uow: uow.products.add_alias(alias))
    before = stored_state(migrated_engine)
    code = parse_item_code(printed, COSTCO_KEY)
    identity = None if code is None else code.identity_at(COSTCO_KEY)
    choices = [
        MatchChoice(
            0,
            MatchSource.CATALOG if printed == CODED_ALIASED else MatchSource.ALIAS,
            product_id=two.s,
            code=identity if printed == CODED_ALIASED else None,
        )
    ]

    def save(uow: UnitOfWork) -> object:
        return record_matches(uow, plan_matches(uow, receipt_id, choices), choices, CREATED_AT)

    if linked is None:
        with pytest.raises(NotFoundError, match=f"product {two.s} does not exist"):
            in_unit(factory, save)
        assert stored_state(migrated_engine) == before
        return
    in_unit(factory, save)
    saved = stored_state(migrated_engine).receipts[receipt_id]
    assert saved.status is ReceiptStatus.COMPLETED
    assert (saved.items[0].product_id, saved.items[0].match_source) == (two.k, linked)
