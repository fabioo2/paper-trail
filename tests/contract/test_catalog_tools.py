"""``catalog_lookup`` and ``describe_from_catalog`` (barcode-lookup PR 1a, R7, A1, A6, A8, A9;
PC-TC-36..42, 92, 93), over the fake and the SQLite unit of work.

Replaces ``test_enrichment_tools.py``: ``save_enrichment`` is removed from the registry (its
rows are owned here, by PC-TC-52 for embedding and by the registry test for its removal).
Calls go through the in-process gateway; one test per tool also runs over MCP (the transports
are otherwise compared in ``test_transport_equivalence.py`` and ``test_cli_tools.py``). The
tools' catalog is the production one: the store catalog of the unit of work, then ``PluCatalog``
over ``PLU_CSV`` in ``tmp_path``.

Pinned (signatures.md, test-cases.md):
- ``catalog_lookup {raw_text, store_key}`` (MATCHING, ENRICHMENT; read-only) ->
  ``{code: {kind, printed, keys, scheme, identity} | null, product: ProductOut | null, entry:
  CatalogEntryPayload | null}``. No leading code: all three null. Kind ``none``: ``code`` set,
  the catalog is not asked. Ours: ``product`` with its real ``field_sources`` and the
  ``store_catalog`` entry. A reference hit: ``product`` null, ``entry`` set.
- ``describe_from_catalog {product_id, fields, sources, alias, store_key}`` (SUPERVISOR) ->
  ``{product}``. Each given field has exactly one source (``size`` covers ``size_value`` and
  ``size_unit``); a stored ``person`` field is kept, re-read inside the write unit (A1); a
  person's CATEGORY correction for the line's alias (either normalization, A8) at that store
  wins with source ``person``; name, brand and tags are masked; nothing is embedded.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from paper_trail.adapters.catalog.plu import PluCatalog
from paper_trail.domain.enums import (
    AgentRole,
    AliasSource,
    Category,
    CodeScheme,
    CodeSource,
    CorrectionKind,
    FieldSource,
    Unit,
)
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import JsonValue, Product, ProductAlias, ProductCode
from paper_trail.ports.llm import LlmUnavailableError
from paper_trail.ports.tools import ToolGateway, ToolInputError
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from paper_trail.tools.inprocess import InProcessToolGateway
from tests.agent_harness import TOOL_TRANSPORTS, Harness, build_harness, mcp_gateway
from tests.extractions import C1, L1, L3, L4, LOBLAWS_KEY, PLU_CSV, U1, WALMART_KEY
from tests.factories import CREATED_AT, MASKED_TEST_PAN, TEST_PAN, at, correction, product
from tests.fakes.catalog import FakeCatalog
from tests.fakes.embeddings import FakeEmbeddings, basis
from tests.fakes.llm import ScriptedLlm
from tests.mcp_server import serve_mcp
from tests.storage import in_unit
from tests.support import require_id

MATCHING = AgentRole.MATCHING
SUPERVISOR = AgentRole.SUPERVISOR
PERSON = FieldSource.PERSON
PLU = FieldSource.PLU
L3_ALIAS = "tst apple"
L3_OLD_ALIAS = "tst apple mrj"
TST_APPLE_MRJ = "TST APPLE MRJ"

CHOC = product(
    canonical_name="Choc Pistachio Bars",
    brand="Kirkland Signature",
    size_value=Decimal("1.2"),
    size_unit=Unit.KG,
    category=Category.SNACKS_CANDY,
    tags=("bars",),
    field_sources={"canonical_name": PERSON},
)


def _printed(name: str) -> Product:
    """A printed-text product, as matching creates one for a line nobody knows."""
    return product(
        canonical_name=name,
        brand=None,
        size_value=None,
        size_unit=None,
        category=None,
        tags=(),
        field_sources={"canonical_name": FieldSource.RECEIPT_TEXT},
    )


@contextmanager
def _gateway(harness: Harness, transport: str) -> Iterator[ToolGateway]:
    if transport == "inprocess":
        yield InProcessToolGateway(harness.registry)
        return
    with serve_mcp(harness.registry) as url:
        yield mcp_gateway(url)


def _harness(uow_factory: UnitOfWorkFactory, tmp_path: Path, **overrides: Any) -> Harness:
    plu = tmp_path / "plu.csv"
    plu.write_text(PLU_CSV, encoding="utf-8")
    values: dict[str, Any] = {
        "uow_factory": uow_factory,
        "embeddings": FakeEmbeddings(),
        "catalog": PluCatalog(plu),
    }
    values.update(overrides)
    return build_harness(ScriptedLlm(), **values)


@pytest.fixture
def harness(uow_factory: UnitOfWorkFactory, tmp_path: Path) -> Harness:
    return _harness(uow_factory, tmp_path)


@pytest.fixture
def gateway(harness: Harness) -> ToolGateway:
    return InProcessToolGateway(harness.registry)


def _add(harness: Harness, stored: Product, *, vector: bool = False) -> int:
    def add(uow: UnitOfWork) -> int:
        product_id = require_id(uow.products.add(stored).id)
        if vector:
            uow.vectors.upsert(product_id, basis(0))
        return product_id

    return in_unit(harness.uow_factory, add)


def _attach(
    harness: Harness, product_id: int, scheme: CodeScheme, store_key: str, code: str
) -> None:
    value = ProductCode(
        None, product_id, scheme, store_key, code, CodeSource.MANUAL, CREATED_AT, CREATED_AT
    )
    in_unit(harness.uow_factory, lambda uow: uow.product_codes.upsert(value, replace=True))


def _remember(  # noqa: PLR0913 - a test builder: keyword-only overrides with defaults
    harness: Harness,
    kind: CorrectionKind,
    subject: str,
    new_value: str,
    *,
    store_key: str = LOBLAWS_KEY,
    minutes: int = 0,
) -> None:
    value = correction(
        kind=kind,
        subject=subject,
        old_value=None,
        new_value=new_value,
        store_key=store_key,
        created_at=at(minutes),
    )
    in_unit(harness.uow_factory, lambda uow: uow.corrections.add(value))


def _snapshot(harness: Harness) -> tuple[object, ...]:
    """Every product, code and alias, and the vector index's answer around basis(0)."""
    with harness.uow_factory(read_only=True) as uow:
        products = [uow.products.get(product_id) for product_id in harness.product_ids()]
        aliases = [uow.products.aliases(product_id) for product_id in harness.product_ids()]
        codes = uow.product_codes.list()
        vectors = uow.vectors.nearest(basis(0), k=10)
    return products, aliases, codes, vectors


def _lookup(gateway: ToolGateway, raw_text: str, store_key: str) -> dict[str, Any]:
    payload: dict[str, JsonValue] = {"raw_text": raw_text, "store_key": store_key}
    return dict(gateway.call(MATCHING, "catalog_lookup", payload))


def _describe(  # noqa: PLR0913 - a test builder: keyword-only overrides with defaults
    gateway: ToolGateway,
    product_id: int,
    fields: dict[str, JsonValue],
    sources: dict[str, JsonValue],
    *,
    alias: str = L3,
    store_key: str = LOBLAWS_KEY,
) -> dict[str, Any]:
    payload: dict[str, JsonValue] = {
        "product_id": product_id,
        "fields": fields,
        "sources": sources,
        "alias": alias,
        "store_key": store_key,
    }
    return dict(gateway.call(SUPERVISOR, "describe_from_catalog", payload))


def _code(
    kind: str, printed: str, keys: list[str], scheme: str | None, identity: str | None
) -> dict[str, Any]:
    return {"kind": kind, "printed": printed, "keys": keys, "scheme": scheme, "identity": identity}


def _plu_entry(name: str, tags: list[str]) -> dict[str, Any]:
    return {
        "source": "plu",
        "key": "3101",
        "name": name,
        "brand": None,
        "size_value": None,
        "size_unit": None,
        "quantity_text": None,
        "categories": [],
        "category": "produce",
        "tags": tags,
    }


# --- catalog_lookup (PC-TC-36, 37) --------------------------------------------------------------


def test_catalog_lookup_of_our_code_returns_the_product_and_the_store_entry(
    harness: Harness, gateway: ToolGateway
) -> None:
    product_id = _add(harness, CHOC)
    _attach(harness, product_id, CodeScheme.STORE, "costco", "1362394")
    before = _snapshot(harness)

    result = _lookup(gateway, C1, "costco")

    assert result == {
        "code": _code("store_item", "1362394", ["1362394"], "store", "1362394"),
        "product": {
            "id": product_id,
            "canonical_name": "Choc Pistachio Bars",
            "brand": "Kirkland Signature",
            "size_value": "1.2",
            "size_unit": "kg",
            "category": "snacks_candy",
            "tags": ["bars"],
            "field_sources": {"canonical_name": "person"},
        },
        "entry": {
            "source": "store_catalog",
            "key": "1362394",
            "name": "Choc Pistachio Bars",
            "brand": "Kirkland Signature",
            "size_value": "1.2",
            "size_unit": "kg",
            "quantity_text": None,
            "categories": [],
            "category": "snacks_candy",
            "tags": ["bars"],
        },
    }
    assert _snapshot(harness) == before


@pytest.mark.parametrize(
    ("raw_text", "expected"),
    [
        (
            L3,
            {
                "code": _code("plu", "3101", ["3101"], "plu", "3101"),
                "product": None,
                "entry": _plu_entry("Test Apple", []),
            },
        ),
        (
            L4,
            {
                "code": _code("plu", "93101", ["3101"], "plu", "93101"),
                "product": None,
                "entry": _plu_entry("Organic Test Apple", ["organic"]),
            },
        ),
        (
            L1,
            {
                "code": _code("gtin", "06038306487", ["0060383064877"], "gtin", "0060383064877"),
                "product": None,
                "entry": None,
            },
        ),
        (U1, {"code": None, "product": None, "entry": None}),
    ],
    ids=["plu", "organic-plu", "unknown-gtin", "no-code"],
)
def test_catalog_lookup_outcomes(
    harness: Harness, gateway: ToolGateway, raw_text: str, expected: dict[str, Any]
) -> None:
    before = _snapshot(harness)

    assert _lookup(gateway, raw_text, LOBLAWS_KEY) == expected
    assert _snapshot(harness) == before


def test_a_code_that_is_not_a_product_code_is_not_looked_up(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    reference = FakeCatalog()
    harness = _harness(uow_factory, tmp_path, catalog=reference)

    result = _lookup(InProcessToolGateway(harness.registry), "51234567890 COUPON", LOBLAWS_KEY)

    assert result == {
        "code": _code("none", "51234567890", [], None, None),
        "product": None,
        "entry": None,
    }
    assert reference.calls == []


def test_catalog_lookup_over_mcp_equals_in_process(harness: Harness) -> None:
    expected = _lookup(InProcessToolGateway(harness.registry), L4, LOBLAWS_KEY)

    with _gateway(harness, "mcp") as gateway:
        assert _lookup(gateway, L4, LOBLAWS_KEY) == expected


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"store_key": LOBLAWS_KEY}, "raw_text"),
        ({"raw_text": C1}, "store_key"),
        ({"raw_text": C1, "store_key": LOBLAWS_KEY, "sneaky": True}, "sneaky"),
    ],
    ids=["no-raw-text", "no-store-key", "extra-field"],
)
def test_catalog_lookup_refuses_bad_input_naming_the_field(
    gateway: ToolGateway, payload: dict[str, JsonValue], field: str
) -> None:
    with pytest.raises(ToolInputError, match=field):
        gateway.call(MATCHING, "catalog_lookup", payload)


# --- describe_from_catalog: fields and their sources (PC-TC-38, 39) ------------------------------


@pytest.mark.parametrize("transport", TOOL_TRANSPORTS)
def test_describe_writes_the_fields_that_come_with_a_source(
    harness: Harness, transport: str
) -> None:
    product_id = _add(harness, _printed("ORG TST APPLE"))
    fields: dict[str, JsonValue] = {
        "name": "Organic Test Apple",
        "category": "produce",
        "tags": ["organic"],
    }
    sources: dict[str, JsonValue] = {"canonical_name": "plu", "category": "plu", "tags": "plu"}

    with _gateway(harness, transport) as gateway:
        first = _describe(gateway, product_id, fields, sources, alias=L4)
        stored = harness.product(product_id)
        again = _describe(gateway, product_id, fields, sources, alias=L4)

    assert stored == replace(
        _printed("Organic Test Apple"),
        id=product_id,
        category=Category.PRODUCE,
        tags=("organic",),
        field_sources={"canonical_name": PLU, "category": PLU, "tags": PLU},
    )
    assert first == {
        "product": {
            "id": product_id,
            "canonical_name": "Organic Test Apple",
            "brand": None,
            "size_value": None,
            "size_unit": None,
            "category": "produce",
            "tags": ["organic"],
            "field_sources": {"canonical_name": "plu", "category": "plu", "tags": "plu"},
        }
    }
    assert again == first
    assert harness.product(product_id) == stored


def test_size_is_one_provenance_key(harness: Harness, gateway: ToolGateway) -> None:
    product_id = _add(harness, _printed("TST APPLE"))

    _describe(gateway, product_id, {"size_value": "1.36", "size_unit": "kg"}, {"size": "plu"})

    stored = harness.product(product_id)
    assert (stored.size_value, stored.size_unit) == (Decimal("1.36"), Unit.KG)
    assert stored.field_sources == {"canonical_name": FieldSource.RECEIPT_TEXT, "size": PLU}


@pytest.mark.parametrize(
    ("fields", "sources", "message"),
    [
        ({"name": "Test Apple"}, {}, "field name has no source canonical_name"),
        (
            {"name": "Test Apple"},
            {"canonical_name": "plu", "size": "plu"},
            "source size has no field",
        ),
        ({"size_unit": "kg"}, {"size": "plu"}, "size"),
        ({"size_value": "1.36", "size_unit": "kg"}, {"size_value": "plu"}, "size_value"),
        ({"size_value": "1.36", "size_unit": "kg"}, {"size_unit": "plu"}, "size_unit"),
        ({"name": "Test Apple"}, {"canonical_name": "banana"}, "canonical_name"),
        ({"name": "Test Apple"}, {"canonical_name": "person"}, "not a data source"),
        ({"category": "produce"}, {"category": "legacy_model"}, "not a data source"),
        ({"name": "Test Apple"}, {"canonical_name": "receipt_text"}, "not a data source"),
        ({"category": "groceries"}, {"category": "plu"}, "category"),
        ({"tags": [f"t{n}" for n in range(9)]}, {"tags": "plu"}, "tags"),
    ],
    ids=[
        "field-without-source",
        "source-without-field",
        "unit-without-value",
        "source-key-size-value",
        "source-key-size-unit",
        "unknown-source",
        "person-source",
        "legacy-model-source",
        "receipt-text-source",
        "unknown-category",
        "nine-tags",
    ],
)
def test_fields_and_sources_must_pair_up(
    harness: Harness,
    gateway: ToolGateway,
    fields: dict[str, JsonValue],
    sources: dict[str, JsonValue],
    message: str,
) -> None:
    product_id = _add(harness, _printed("TST APPLE"))
    before = _snapshot(harness)

    with pytest.raises(ToolInputError, match=message):
        _describe(gateway, product_id, fields, sources)

    assert _snapshot(harness) == before


def test_an_extra_field_is_refused(harness: Harness, gateway: ToolGateway) -> None:
    product_id = _add(harness, _printed("TST APPLE"))
    before = _snapshot(harness)
    payload: dict[str, JsonValue] = {
        "product_id": product_id,
        "fields": {"category": "produce"},
        "sources": {"category": "plu"},
        "alias": L3,
        "store_key": LOBLAWS_KEY,
        "confidence": 0.9,
    }

    with pytest.raises(ToolInputError, match="confidence"):
        gateway.call(SUPERVISOR, "describe_from_catalog", payload)

    assert _snapshot(harness) == before


def test_an_unknown_product_is_not_found_and_nothing_is_written(
    harness: Harness, gateway: ToolGateway
) -> None:
    product_id = _add(harness, _printed("TST APPLE"))
    before = _snapshot(harness)

    with pytest.raises(NotFoundError, match=f"product {product_id + 100} does not exist"):
        _describe(gateway, product_id + 100, {"category": "produce"}, {"category": "plu"})

    assert _snapshot(harness) == before


# --- a person's fields are never replaced (PC-TC-40, A1) ---------------------------------------

FIELDS = ("canonical_name", "brand", "size", "category", "tags")
OLD = product(
    canonical_name="OLD NAME",
    brand="Old Brand",
    size_value=Decimal("2"),
    size_unit=Unit.EACH,
    category=Category.PANTRY,
    tags=("old",),
)
DESCRIBED: dict[str, JsonValue] = {
    "name": "Organic Test Apple",
    "brand": "Acme",
    "size_value": "1.36",
    "size_unit": "kg",
    "category": "produce",
    "tags": ["organic"],
}
NEW = replace(
    OLD,
    canonical_name="Organic Test Apple",
    brand="Acme",
    size_value=Decimal("1.36"),
    size_unit=Unit.KG,
    category=Category.PRODUCE,
    tags=("organic",),
)


def _field_values(source: Product, key: str) -> dict[str, Any]:
    names = {
        "canonical_name": ("canonical_name",),
        "brand": ("brand",),
        "size": ("size_value", "size_unit"),
        "category": ("category",),
        "tags": ("tags",),
    }[key]
    return {name: getattr(source, name) for name in names}


def _with_sources(kept: str, kept_source: FieldSource) -> Product:
    """OLD with every field sourced ``receipt_text`` but ``kept``."""
    sources = dict.fromkeys(FIELDS, FieldSource.RECEIPT_TEXT)
    sources[kept] = kept_source
    return replace(OLD, field_sources=sources)


def _expected(product_id: int, kept: str | None) -> Product:
    sources = dict.fromkeys(FIELDS, PLU)
    expected = replace(NEW, id=product_id)
    if kept is not None:
        sources[kept] = PERSON
        expected = replace(expected, **_field_values(OLD, kept))
    return replace(expected, field_sources=sources)


# A person's value is kept per field (each field once); any other source is replaced, which one
# field per source shows (the rule does not depend on the field).
KEPT_OR_WRITTEN = [
    *((field, PERSON) for field in FIELDS),
    ("brand", FieldSource.RECEIPT_TEXT),
    ("category", FieldSource.LEGACY_MODEL),
    ("tags", PLU),
]


@pytest.mark.parametrize(("field", "existing"), KEPT_OR_WRITTEN, ids=str)
def test_a_person_field_is_kept_and_every_other_is_written(
    harness: Harness, gateway: ToolGateway, field: str, existing: FieldSource
) -> None:
    product_id = _add(harness, _with_sources(field, existing))

    _describe(gateway, product_id, DESCRIBED, dict.fromkeys(FIELDS, "plu"))

    kept = field if existing is PERSON else None
    assert harness.product(product_id) == _expected(product_id, kept)


class _PersonWritesFirst:
    """A unit-of-work factory that, armed, lets a person write one field just before the next
    writing unit opens: after the caller read the product, before the tool's write (A1)."""

    def __init__(self, inner: UnitOfWorkFactory) -> None:
        self._inner = inner
        self.write: Callable[[UnitOfWork], None] | None = None
        self.fired = False

    def __call__(self, *, read_only: bool = False) -> UnitOfWork:
        if not read_only and self.write is not None:
            write, self.write = self.write, None
            in_unit(self._inner, write)
            self.fired = True
        return self._inner(read_only=read_only)


def test_a_field_a_person_wrote_meanwhile_is_reread_and_kept(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """A1: one field shows the re-read; which fields a person keeps is the test above."""
    field = "category"
    hooked = _PersonWritesFirst(uow_factory)
    harness = _harness(hooked, tmp_path)
    product_id = _add(harness, _with_sources(field, FieldSource.RECEIPT_TEXT))
    harness.product(product_id)

    def person_writes(uow: UnitOfWork) -> None:
        current = uow.products.get(product_id)
        assert current is not None
        sources = {**current.field_sources, field: PERSON}
        uow.products.update(replace(current, field_sources=sources))

    hooked.write = person_writes
    _describe(
        InProcessToolGateway(harness.registry), product_id, DESCRIBED, dict.fromkeys(FIELDS, "plu")
    )

    assert hooked.fired
    assert harness.product(product_id) == _expected(product_id, field)


# --- a person's category correction (PC-TC-41) --------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "sources"),
    [
        ({"category": "produce"}, {"category": "plu"}),
        ({"name": "Test Apple"}, {"canonical_name": "plu"}),
    ],
    ids=["described-category", "no-category-given"],
)
def test_a_category_correction_for_the_line_at_the_store_wins(
    harness: Harness,
    gateway: ToolGateway,
    fields: dict[str, JsonValue],
    sources: dict[str, JsonValue],
) -> None:
    product_id = _add(harness, _printed("TST APPLE"))
    _remember(harness, CorrectionKind.CATEGORY, L3_ALIAS, "pantry", minutes=0)
    _remember(harness, CorrectionKind.CATEGORY, L3_ALIAS, "dairy_eggs", minutes=5)

    result = _describe(gateway, product_id, fields, sources)

    stored = harness.product(product_id)
    assert (stored.category, stored.field_sources["category"]) == (Category.DAIRY_EGGS, PERSON)
    assert result["product"]["category"] == "dairy_eggs"


@pytest.mark.parametrize(
    ("kind", "subject", "store_key"),
    [
        (CorrectionKind.CATEGORY, L3_ALIAS, WALMART_KEY),
        (CorrectionKind.CATEGORY, "ks sourdough", LOBLAWS_KEY),
        (CorrectionKind.PRODUCT_LINK, L3_ALIAS, LOBLAWS_KEY),
    ],
    ids=["another-store", "another-alias", "not-a-category"],
)
def test_other_corrections_do_not_count(
    harness: Harness,
    gateway: ToolGateway,
    kind: CorrectionKind,
    subject: str,
    store_key: str,
) -> None:
    product_id = _add(harness, _printed("TST APPLE"))
    _remember(harness, kind, subject, "dairy_eggs", store_key=store_key)

    _describe(gateway, product_id, {"category": "produce"}, {"category": "plu"})

    stored = harness.product(product_id)
    assert (stored.category, stored.field_sources["category"]) == (Category.PRODUCE, PLU)


def test_a_category_correction_does_not_replace_a_persons_category(
    harness: Harness, gateway: ToolGateway
) -> None:
    """Fable P3-5 (A1): a category a person gave the product itself is kept, even against a
    category correction for the line."""
    product_id = _add(
        harness,
        replace(
            _printed("TST APPLE"),
            category=Category.SNACKS_CANDY,
            field_sources={"canonical_name": FieldSource.RECEIPT_TEXT, "category": PERSON},
        ),
    )
    _remember(harness, CorrectionKind.CATEGORY, L3_ALIAS, "dairy_eggs")

    _describe(gateway, product_id, {"category": "produce"}, {"category": "plu"})

    stored = harness.product(product_id)
    assert (stored.category, stored.field_sources["category"]) == (Category.SNACKS_CANDY, PERSON)


# --- masking (PC-TC-42) and no embedding (PC-TC-93) -----------------------------------------------


def test_free_text_is_masked(harness: Harness, gateway: ToolGateway) -> None:
    product_id = _add(harness, _printed("TST APPLE"))

    result = _describe(
        gateway,
        product_id,
        {"name": f"Gift card {TEST_PAN}", "brand": f"Brand {TEST_PAN}", "tags": [TEST_PAN]},
        {"canonical_name": "plu", "brand": "plu", "tags": "plu"},
    )

    stored = harness.product(product_id)
    assert (stored.canonical_name, stored.brand, stored.tags) == (
        f"Gift card {MASKED_TEST_PAN}",
        f"Brand {MASKED_TEST_PAN}",
        (MASKED_TEST_PAN,),
    )
    digits = TEST_PAN.replace(" ", "")
    assert digits not in repr(stored).replace(" ", "")
    assert digits not in repr(result).replace(" ", "")


@pytest.mark.parametrize(
    ("stored", "fields", "sources", "vector", "described"),
    [
        (
            _printed("TST APPLE"),
            {"name": "Test Apple"},
            {"canonical_name": "plu"},
            False,
            {"canonical_name": PLU},
        ),
        (
            replace(_printed("Test Apple"), field_sources={"canonical_name": PERSON}),
            {"category": "produce"},
            {"category": "plu"},
            True,
            {"canonical_name": PERSON, "category": PLU},
        ),
    ],
    ids=["unconfirmed-becomes-confirmed", "already-confirmed"],
)
def test_describe_never_embeds(  # noqa: PLR0913, PLR0917 - the fixtures and one table row
    uow_factory: UnitOfWorkFactory,
    tmp_path: Path,
    stored: Product,
    fields: dict[str, JsonValue],
    sources: dict[str, JsonValue],
    vector: bool,
    described: dict[str, FieldSource],
) -> None:
    embeddings = FakeEmbeddings(error=LlmUnavailableError("cannot reach llm-host.test:11434"))
    harness = _harness(uow_factory, tmp_path, embeddings=embeddings)
    product_id = _add(harness, stored, vector=vector)
    with harness.uow_factory(read_only=True) as uow:
        vectors = uow.vectors.nearest(basis(0), k=10)

    _describe(InProcessToolGateway(harness.registry), product_id, fields, sources)

    assert harness.product(product_id).field_sources == described
    assert embeddings.calls == 0
    with harness.uow_factory(read_only=True) as uow:
        assert uow.vectors.nearest(basis(0), k=10) == vectors
    assert harness.has_vector(product_id) is vector


# --- old and new normalization keys (PC-TC-92, A8) ------------------------------------------------


def _product_lookup(gateway: ToolGateway) -> tuple[object, object]:
    """(product id or None, source) of ``product_lookup`` for the line ``TST APPLE MRJ``."""
    payload: dict[str, JsonValue] = {"alias": TST_APPLE_MRJ, "store_key": LOBLAWS_KEY}
    result = gateway.call(MATCHING, "product_lookup", payload)
    found = result["product"]
    return (found["id"] if isinstance(found, dict) else None), result["source"]


@pytest.mark.parametrize(
    ("corrections", "winner"),
    [
        ([(L3_OLD_ALIAS, 0)], 0),
        ([(L3_OLD_ALIAS, 5), (L3_ALIAS, 0)], 0),
        ([(L3_OLD_ALIAS, 0), (L3_ALIAS, 5)], 1),
    ],
    ids=["only-old-key", "old-key-newer", "new-key-newer"],
)
def test_the_newest_link_correction_wins_across_both_keys(
    harness: Harness,
    gateway: ToolGateway,
    corrections: list[tuple[str, int]],
    winner: int,
) -> None:
    ids = [_add(harness, product(canonical_name=f"P{n}")) for n in range(len(corrections))]
    for product_id, (subject, minutes) in zip(ids, corrections, strict=True):
        _remember(harness, CorrectionKind.PRODUCT_LINK, subject, str(product_id), minutes=minutes)

    assert _product_lookup(gateway) == (ids[winner], "memory")


def test_each_kind_takes_its_own_newest_correction(harness: Harness, gateway: ToolGateway) -> None:
    linked = _add(harness, product(canonical_name="Linked"))
    described = _add(harness, _printed("TST APPLE"))
    _remember(harness, CorrectionKind.PRODUCT_LINK, L3_ALIAS, str(linked), minutes=0)
    _remember(harness, CorrectionKind.CATEGORY, L3_OLD_ALIAS, "dairy_eggs", minutes=5)

    found = _product_lookup(gateway)
    _describe(gateway, described, {"category": "produce"}, {"category": "plu"})

    assert found == (linked, "memory")
    assert harness.product(described).category is Category.DAIRY_EGGS


def test_an_old_key_correction_at_another_store_is_never_used(
    harness: Harness, gateway: ToolGateway
) -> None:
    described = _add(harness, _printed("TST APPLE"))
    _remember(harness, CorrectionKind.CATEGORY, L3_OLD_ALIAS, "dairy_eggs", store_key=WALMART_KEY)

    _describe(gateway, described, {"category": "produce"}, {"category": "plu"})

    assert harness.product(described).category is Category.PRODUCE


@pytest.mark.parametrize(
    ("aliases", "winner"),
    [
        ([(L3_OLD_ALIAS, AliasSource.USER_CORRECTION, 0), (L3_ALIAS, AliasSource.MATCHED, 5)], 0),
        ([(L3_OLD_ALIAS, AliasSource.MATCHED, 5), (L3_ALIAS, AliasSource.IMPORT, 0)], 1),
        ([(L3_OLD_ALIAS, AliasSource.MATCHED, 5), (L3_ALIAS, AliasSource.MATCHED, 0)], 0),
        ([(L3_OLD_ALIAS, AliasSource.MATCHED, 0), (L3_ALIAS, AliasSource.MATCHED, 5)], 1),
    ],
    ids=["person-old-key", "person-new-key", "old-key-newer", "new-key-newer"],
)
def test_aliases_follow_the_same_rule_person_first_then_newest(
    harness: Harness,
    gateway: ToolGateway,
    aliases: list[tuple[str, AliasSource, int]],
    winner: int,
) -> None:
    ids = [_add(harness, product(canonical_name=f"P{n}")) for n in range(len(aliases))]

    def add(uow: UnitOfWork) -> None:
        for product_id, (alias, source, minutes) in zip(ids, aliases, strict=True):
            uow.products.add_alias(
                ProductAlias(None, product_id, alias, LOBLAWS_KEY, source, at(minutes))
            )

    in_unit(harness.uow_factory, add)

    assert _product_lookup(gateway) == (ids[winner], "alias")
