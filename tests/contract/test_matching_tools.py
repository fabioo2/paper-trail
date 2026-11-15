"""The matching tools, on every transport and storage (PLAN 6, PLAN 7, AC-C-01, AC-C-02).

``product_lookup`` and ``product_similarity_search`` (the matching agent's read tools) and
``save_matches`` (the supervisor's write tool) are called through the in-process gateway and
over MCP, each over the fake and the SQLite unit of work (real sqlite-vec index). Embeddings are
``FakeEmbeddings``; nothing contacts a model.

Pinned (docs/tests/agent-core-C.md):
- ``ToolServices`` gains ``embeddings: EmbeddingPort``.
- ``product_lookup {alias: str, store_key: str | null = null}`` ->
  ``{product: ProductOut | null, source: "memory" | "alias" | null}``. The tool normalizes
  ``alias`` (``normalize_alias``, idempotent). Correction memory first: the newest PRODUCT_LINK
  correction for exactly (alias, store_key) names the product by id in ``new_value``; then the
  exact alias, the store's own before a global one. ``ProductOut`` = ``{id, canonical_name,
  brand, size_value: str | null, size_unit, category, tags}``.
- ``product_similarity_search {text: str, k: int 1..10}`` -> ``{candidates: [{product:
  ProductOut, distance: float}]}``: embeds ``text`` as given (one call, one text), then
  ``VectorIndex.nearest``; ascending distance, at most ``k``. ``LlmUnavailableError`` from the
  embeddings propagates, over MCP too (``tools.wire`` gains the kind ``llm_unavailable``).
- ``save_matches {receipt_id, decisions: [{position, source, product_id: int | null,
  new_product_name: str | null}]}`` -> ``{receipt_id, linked, new_products, prices, lines:
  [{position, product_id, source}]}``. A decision has exactly one of ``product_id`` and
  ``new_product_name``, and ``source`` is ``new`` exactly when it names a new product; anything
  else is a ``ToolInputError``. In one unit of work: new products (canonical name as given; one
  product per distinct line alias among the new decisions), ``link_items`` with each decision's
  source, a MATCHED alias (the line's normalized raw text, the receipt's store) for every linked
  line (a USER_CORRECTION alias is kept), one vector per new product (the embedding of its
  alias, all embedded in one call, no call without new products), and one price observation
  per linked ITEM line when the purchase date is known. Unknown receipt, position or product
  -> NotFoundError, and nothing is written; so is a failed embedding call.
- ``save_matches`` is idempotent: the same decisions again (a run resumed after a crash between
  the tool's commit and the checkpoint) leave the same products, links, aliases and prices.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest
from structlog.testing import capture_logs

from paper_trail.application.payloads import DraftPayload
from paper_trail.domain.enums import (
    AgentRole,
    AliasSource,
    CodeScheme,
    CodeSource,
    CorrectionKind,
    FieldSource,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
)
from paper_trail.domain.errors import DomainValidationError, NotFoundError
from paper_trail.domain.models import CodeIdentity, JsonValue, ProductAlias, ProductCode
from paper_trail.ports.llm import LlmUnavailableError
from paper_trail.ports.tools import ToolGateway, ToolInputError
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from paper_trail.tools.inprocess import InProcessToolGateway
from tests.agent_harness import TOOL_TRANSPORTS, Harness, build_harness, mcp_gateway
from tests.extractions import (
    C1,
    C2,
    EGGS_ALIAS,
    L1,
    L3,
    L4,
    LOBLAWS,
    LOBLAWS_KEY,
    MILK_ALIAS,
    STORE_KEY,
    STORE_NAME,
    U1,
    WALMART_KEY,
)
from tests.factories import CREATED_AT, correction, line_item, product, receipt, receipt_draft
from tests.fakes.embeddings import FakeEmbeddings, at_distance, basis
from tests.fakes.llm import ScriptedLlm
from tests.mcp_server import serve_mcp
from tests.storage import in_unit
from tests.support import require_id

MATCHING = AgentRole.MATCHING
SUPERVISOR = AgentRole.SUPERVISOR
TABLE = {EGGS_ALIAS: basis(0), MILK_ALIAS: basis(5)}
PRODUCT_KEYS = {
    "id",
    "canonical_name",
    "brand",
    "size_value",
    "size_unit",
    "category",
    "tags",
    "field_sources",
}


@contextmanager
def _gateway(harness: Harness, transport: str) -> Iterator[ToolGateway]:
    if transport == "inprocess":
        yield InProcessToolGateway(harness.registry)
        return
    with serve_mcp(harness.registry) as url:
        yield mcp_gateway(url)


@pytest.fixture(params=TOOL_TRANSPORTS)
def transport_name(request: pytest.FixtureRequest) -> str:
    name: str = request.param
    return name


@pytest.fixture
def harness(uow_factory: UnitOfWorkFactory) -> Harness:
    return build_harness(ScriptedLlm(), uow_factory=uow_factory, embeddings=FakeEmbeddings(TABLE))


@pytest.fixture
def gateway(harness: Harness, transport_name: str) -> Iterator[ToolGateway]:
    with _gateway(harness, transport_name) as gateway:
        yield gateway


def _product(harness: Harness, name: str, vector: tuple[float, ...] | None = None) -> int:
    def add(uow: UnitOfWork) -> int:
        product_id = require_id(uow.products.add(product(canonical_name=name)).id)
        if vector is not None:
            uow.vectors.upsert(product_id, vector)
        return product_id

    return in_unit(harness.uow_factory, add)


def _alias(
    harness: Harness,
    product_id: int,
    alias: str,
    store_key: str | None,
    source: AliasSource = AliasSource.MATCHED,
) -> None:
    value = ProductAlias(None, product_id, alias, store_key, source, CREATED_AT)
    in_unit(harness.uow_factory, lambda uow: uow.products.add_alias(value))


def _remember_link(harness: Harness, alias: str, store_key: str | None, product_id: int) -> None:
    value = correction(
        kind=CorrectionKind.PRODUCT_LINK,
        subject=alias,
        old_value=None,
        new_value=str(product_id),
        store_key=store_key,
    )
    in_unit(harness.uow_factory, lambda uow: uow.corrections.add(value))


def _lookup(gateway: ToolGateway, alias: str, store_key: str | None) -> dict[str, Any]:
    return dict(gateway.call(MATCHING, "product_lookup", {"alias": alias, "store_key": store_key}))


def _found(result: dict[str, Any]) -> tuple[int | None, str | None]:
    found = result["product"]
    return (None if found is None else found["id"]), result["source"]


# --- product_lookup -----------------------------------------------------------------------------


def test_lookup_finds_the_stores_alias_and_normalizes_the_input(
    harness: Harness, gateway: ToolGateway
) -> None:
    eggs = _product(harness, "Kirkland Organic Eggs 24")
    _alias(harness, eggs, EGGS_ALIAS, STORE_KEY)

    result = _lookup(gateway, "1234567 KS ORG EGGS 24CT 8.99 H", STORE_KEY)

    assert _found(result) == (eggs, "alias")
    assert set(result["product"]) == PRODUCT_KEYS
    assert result["product"]["canonical_name"] == "Kirkland Organic Eggs 24"


def test_lookup_uses_the_stores_alias_only(harness: Harness, gateway: ToolGateway) -> None:
    """Barcode-lookup A10 (PC-TC-56): a global alias never names a product at a store."""
    store, anywhere = _product(harness, "at costco"), _product(harness, "anywhere")
    _alias(harness, anywhere, EGGS_ALIAS, None)
    _alias(harness, store, EGGS_ALIAS, STORE_KEY)

    assert _found(_lookup(gateway, EGGS_ALIAS, STORE_KEY)) == (store, "alias")
    assert _found(_lookup(gateway, EGGS_ALIAS, WALMART_KEY)) == (None, None)
    assert _found(_lookup(gateway, EGGS_ALIAS, None)) == (anywhere, "alias")


def test_lookup_consults_correction_memory_first(harness: Harness, gateway: ToolGateway) -> None:
    """PLAN 6 step 1: a person's correction wins over any alias, at that store only."""
    aliased, corrected = _product(harness, "aliased"), _product(harness, "corrected")
    _alias(harness, aliased, EGGS_ALIAS, STORE_KEY)
    _alias(harness, aliased, EGGS_ALIAS, WALMART_KEY)
    _remember_link(harness, EGGS_ALIAS, STORE_KEY, corrected)

    assert _found(_lookup(gateway, EGGS_ALIAS, STORE_KEY)) == (corrected, "memory")
    assert _found(_lookup(gateway, EGGS_ALIAS, WALMART_KEY)) == (aliased, "alias")


def test_lookup_without_a_match(gateway: ToolGateway) -> None:
    assert _lookup(gateway, EGGS_ALIAS, STORE_KEY) == {"product": None, "source": None}


# --- product_similarity_search ------------------------------------------------------------------


def test_similarity_search_embeds_the_text_and_ranks_by_distance(
    harness: Harness, gateway: ToolGateway
) -> None:
    near = _product(harness, "near", at_distance(0.05))
    middle = _product(harness, "middle", at_distance(0.2, toward=2))
    _product(harness, "far", at_distance(0.6, toward=3))

    result = gateway.call(MATCHING, "product_similarity_search", {"text": EGGS_ALIAS, "k": 2})

    assert harness.embeddings.requests == [(EGGS_ALIAS,)]
    candidates = result["candidates"]
    assert isinstance(candidates, list)
    assert [_candidate(entry)[0] for entry in candidates] == [near, middle]
    distances = [_candidate(entry)[1] for entry in candidates]
    assert distances == pytest.approx([0.05, 0.2], abs=1e-4)


def _candidate(entry: JsonValue) -> tuple[int, float]:
    assert isinstance(entry, dict)
    found, distance = entry["product"], entry["distance"]
    assert isinstance(found, dict)
    assert set(found) == PRODUCT_KEYS
    assert isinstance(found["id"], int)
    assert isinstance(distance, float)
    return found["id"], distance


def test_similarity_search_without_vectors_has_no_candidates(gateway: ToolGateway) -> None:
    result = gateway.call(MATCHING, "product_similarity_search", {"text": EGGS_ALIAS, "k": 3})

    assert result == {"candidates": []}


@pytest.mark.parametrize("k", [0, 11])
def test_similarity_search_k_is_bounded(gateway: ToolGateway, k: int) -> None:
    with pytest.raises(ToolInputError, match="k"):
        gateway.call(MATCHING, "product_similarity_search", {"text": EGGS_ALIAS, "k": k})


def test_s4_ac_08_an_unavailable_embedding_host_keeps_its_class(
    uow_factory: UnitOfWorkFactory, transport_name: str
) -> None:
    """S3-AC-10 over MCP too: the run must stop with its checkpoint, not fail the receipt."""
    down = FakeEmbeddings(error=LlmUnavailableError("cannot reach llm-host.test:11434"))
    harness = build_harness(ScriptedLlm(), uow_factory=uow_factory, embeddings=down)

    with (
        _gateway(harness, transport_name) as gateway,
        pytest.raises(LlmUnavailableError, match=r"llm-host\.test:11434"),
    ):
        gateway.call(MATCHING, "product_similarity_search", {"text": EGGS_ALIAS, "k": 3})


# --- save_matches -------------------------------------------------------------------------------


def _stored_receipt(harness: Harness, **overrides: Any) -> int:
    value = receipt(**overrides)
    return require_id(in_unit(harness.uow_factory, lambda uow: uow.receipts.add(value)).id)


def _decision(
    position: int, source: MatchSource, product_id: int | None = None, name: str | None = None
) -> dict[str, JsonValue]:
    return {
        "position": position,
        "source": source.value,
        "product_id": product_id,
        "new_product_name": name,
    }


def _save(
    gateway: ToolGateway, receipt_id: int, *decisions: dict[str, JsonValue]
) -> dict[str, Any]:
    payload: dict[str, JsonValue] = {"receipt_id": receipt_id, "decisions": list(decisions)}
    return dict(gateway.call(SUPERVISOR, "save_matches", payload))


def _linked(harness: Harness, receipt_id: int) -> list[tuple[int | None, MatchSource | None]]:
    return [(item.product_id, item.match_source) for item in harness.receipt(receipt_id).items]


def test_save_matches_links_creates_remembers_and_prices(
    harness: Harness, gateway: ToolGateway
) -> None:
    """A new product named from its printed text is not confirmed: no vector (PC-TC-52)."""
    eggs = _product(harness, "Kirkland Organic Eggs 24")
    receipt_id = _stored_receipt(harness)

    result = _save(
        gateway,
        receipt_id,
        _decision(0, MatchSource.ALIAS, product_id=eggs),
        _decision(1, MatchSource.NEW, name="Kirkland 2% Milk 4 L"),
    )

    (milk,) = [pid for pid in harness.product_ids() if pid != eggs]
    assert result == {
        "receipt_id": receipt_id,
        "linked": 2,
        "new_products": 1,
        "prices": 2,
        "lines": [
            {"position": 0, "product_id": eggs, "source": "alias"},
            {"position": 1, "product_id": milk, "source": "new"},
        ],
        "unpriced": [],
    }
    assert _linked(harness, receipt_id) == [(eggs, MatchSource.ALIAS), (milk, MatchSource.NEW)]
    assert harness.product(milk).canonical_name == "Kirkland 2% Milk 4 L"
    assert harness.find_by_alias(EGGS_ALIAS, STORE_KEY) == harness.product(eggs)
    assert harness.find_by_alias(MILK_ALIAS, STORE_KEY) == harness.product(milk)
    assert {alias.source for alias in harness.aliases(milk)} == {AliasSource.MATCHED}
    assert harness.embeddings.calls == 0
    assert not harness.has_vector(milk)
    for product_id in (eggs, milk):
        (price,) = harness.price_history(product_id)
        assert (price.receipt_id, price.store_key) == (receipt_id, STORE_KEY)


def test_save_matches_without_new_products_embeds_nothing(
    harness: Harness, gateway: ToolGateway
) -> None:
    eggs = _product(harness, "eggs")
    receipt_id = _stored_receipt(harness)

    result = _save(gateway, receipt_id, _decision(0, MatchSource.VECTOR, product_id=eggs))

    assert (result["linked"], result["new_products"]) == (1, 0)
    assert harness.embeddings.calls == 0


def test_save_matches_creates_one_product_per_alias(harness: Harness, gateway: ToolGateway) -> None:
    """Two lines with the same text on one receipt are one new product, not two."""
    twice = (line_item(), line_item(position=1))
    receipt_id = _stored_receipt(harness, items=twice, subtotal=None, total=None, tax=None)

    result = _save(
        gateway,
        receipt_id,
        _decision(0, MatchSource.NEW, name="Eggs"),
        _decision(1, MatchSource.NEW, name="Eggs"),
    )

    assert result["new_products"] == 1
    (only,) = harness.product_ids()
    assert _linked(harness, receipt_id) == [(only, MatchSource.NEW), (only, MatchSource.NEW)]


def test_save_matches_keeps_a_users_alias(harness: Harness, gateway: ToolGateway) -> None:
    corrected = _product(harness, "corrected")
    _alias(harness, corrected, EGGS_ALIAS, STORE_KEY, AliasSource.USER_CORRECTION)
    receipt_id = _stored_receipt(harness)

    _save(gateway, receipt_id, _decision(0, MatchSource.MEMORY, product_id=corrected))

    (alias,) = harness.aliases(corrected)
    assert alias.source is AliasSource.USER_CORRECTION


def test_save_matches_records_no_price_without_a_date(
    harness: Harness, gateway: ToolGateway
) -> None:
    eggs = _product(harness, "eggs")
    receipt_id = _stored_receipt(harness, purchased_on=None)

    result = _save(gateway, receipt_id, _decision(0, MatchSource.ALIAS, product_id=eggs))

    assert result["prices"] == 0
    assert harness.price_history(eggs) == ()


def test_save_matches_is_idempotent(harness: Harness, gateway: ToolGateway) -> None:
    eggs = _product(harness, "eggs")
    receipt_id = _stored_receipt(harness, purchased_on=date(2026, 11, 1))
    decisions = (
        _decision(0, MatchSource.ALIAS, product_id=eggs),
        _decision(1, MatchSource.NEW, name="Milk"),
    )

    _save(gateway, receipt_id, *decisions)
    products, links = harness.product_ids(), _linked(harness, receipt_id)
    _save(gateway, receipt_id, *decisions)

    assert harness.product_ids() == products
    assert _linked(harness, receipt_id) == links
    assert [len(harness.price_history(pid)) for pid in products] == [1, 1]
    assert [len(harness.aliases(pid)) for pid in products] == [1, 1]


@pytest.mark.parametrize(
    "decision",
    [
        _decision(9, MatchSource.ALIAS, product_id=1),
        _decision(0, MatchSource.ALIAS, product_id=999),
    ],
    ids=["position", "product"],
)
def test_save_matches_refuses_what_does_not_exist_and_writes_nothing(
    harness: Harness, gateway: ToolGateway, decision: dict[str, JsonValue]
) -> None:
    _product(harness, "eggs")
    receipt_id = _coded_receipt(harness, U1, C1, store=COSTCO)

    with pytest.raises(NotFoundError):
        _save(gateway, receipt_id, _new_from_code(1, "CHOC PISTACH", C1_CODE), decision)

    assert harness.product_ids() == [1]
    assert _linked(harness, receipt_id) == [(None, None), (None, None)]
    assert _codes(harness) == []


def test_save_matches_on_an_unknown_receipt(gateway: ToolGateway) -> None:
    with pytest.raises(NotFoundError):
        _save(gateway, 404, _decision(0, MatchSource.NEW, name="Milk"))


@pytest.mark.parametrize(
    "decision",
    [
        _decision(0, MatchSource.NEW),
        _decision(0, MatchSource.ALIAS, product_id=1, name="both"),
        _decision(0, MatchSource.NEW, product_id=1),
        _decision(0, MatchSource.ALIAS, name="named but not new"),
        _decision(0, MatchSource.CATALOG),
        _decision(0, MatchSource.CATALOG, name="a new name"),
        {**_decision(0, MatchSource.NEW, name="Eggs"), "name_source": "person"},
        {**_decision(0, MatchSource.NEW, name="Eggs"), "name_source": "off"},
    ],
    ids=[
        "neither",
        "both",
        "new-with-product",
        "named-not-new",
        "catalog-without-product",
        "catalog-with-a-new-name",
        "named-by-a-person",
        "named-by-a-source-not-in-use",
    ],
)
def test_save_matches_refuses_an_inconsistent_decision(
    harness: Harness, gateway: ToolGateway, decision: dict[str, JsonValue]
) -> None:
    _product(harness, "eggs")
    receipt_id = _stored_receipt(harness)

    with pytest.raises(ToolInputError):
        _save(gateway, receipt_id, decision)


def test_ac_c_02_a_failed_embedding_writes_nothing(
    uow_factory: UnitOfWorkFactory, transport_name: str
) -> None:
    """AC-C-02, PC-TC-52: a confirmed new product is embedded in the same unit of work as the
    links; when the embedding fails nothing is written, codes included."""
    down = FakeEmbeddings(error=LlmUnavailableError("cannot reach llm-host.test:11434"))
    harness = build_harness(ScriptedLlm(), uow_factory=uow_factory, embeddings=down)
    eggs = _product(harness, "eggs")
    receipt_id = _coded_receipt(harness, U1, L3)

    with _gateway(harness, transport_name) as gateway, pytest.raises(LlmUnavailableError):
        _save(
            gateway,
            receipt_id,
            _decision(0, MatchSource.ALIAS, product_id=eggs),
            _new_from_code(1, "Test Apple", PLU_3101, FieldSource.PLU),
        )

    assert harness.product_ids() == [eggs]
    assert _linked(harness, receipt_id) == [(None, None), (None, None)]
    assert harness.aliases(eggs) == ()
    assert harness.price_history(eggs) == ()
    assert _codes(harness) == []


# --- slice C review note 1 (pinned in docs/tests/agent-core-D.md) ------------------------------


def test_review_note_1_save_receipt_leaves_an_accepted_draft_matching(
    harness: Harness, gateway: ToolGateway
) -> None:
    """The graph's save is not the end of the receipt: it stays PROCESSING/MATCHING until
    ``save_matches`` has linked its lines."""
    receipt_id = _stored_receipt(
        harness, status=ReceiptStatus.PROCESSING, stage=ProcessingStage.READING, items=()
    )
    draft: JsonValue = DraftPayload.from_draft(receipt_draft()).model_dump(mode="json")

    result = gateway.call(SUPERVISOR, "save_receipt", {"receipt_id": receipt_id, "draft": draft})

    assert (result["status"], result["stage"]) == ("processing", "matching")
    stored = harness.receipt(receipt_id)
    assert (stored.status, stored.stage) == (ReceiptStatus.PROCESSING, ProcessingStage.MATCHING)
    assert len(stored.items) == 2


def test_review_note_1_save_matches_completes_a_matching_receipt(
    harness: Harness, gateway: ToolGateway
) -> None:
    receipt_id = _stored_receipt(
        harness, status=ReceiptStatus.PROCESSING, stage=ProcessingStage.MATCHING
    )

    _save(gateway, receipt_id, _decision(0, MatchSource.NEW, name="Kirkland Organic Eggs 24"))

    stored = harness.receipt(receipt_id)
    assert (stored.status, stored.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)


# --- barcode-lookup PR 1a: codes in save_matches (PC-TC-46..53) --------------------------------

COSTCO = (STORE_NAME, STORE_KEY)
LOBLAWS_STORE = (LOBLAWS, LOBLAWS_KEY)
C1_CODE = CodeIdentity(CodeScheme.STORE, STORE_KEY, "1362394")
PLU_3101 = CodeIdentity(CodeScheme.PLU, "", "3101")
PLU_93101 = CodeIdentity(CodeScheme.PLU, "", "93101")
L1_CODE = CodeIdentity(CodeScheme.GTIN, "", "0060383064877")

type CodeRow = tuple[CodeScheme, str, str, int, CodeSource]


def _coded_receipt(
    harness: Harness, *raw_texts: str, store: tuple[str, str] = LOBLAWS_STORE
) -> int:
    items = tuple(
        line_item(position=position, raw_text=text, description=text.title())
        for position, text in enumerate(raw_texts)
    )
    return _stored_receipt(
        harness,
        store_name=store[0],
        store_key=store[1],
        items=items,
        subtotal=None,
        tax=None,
        total=None,
    )


def _ref(code: CodeIdentity) -> dict[str, JsonValue]:
    return {"scheme": code.scheme.value, "store_key": code.store_key, "code": code.code}


def _entry(name: str, tags: tuple[str, ...] = ()) -> dict[str, JsonValue]:
    """A PLU entry as ``catalog_lookup`` returns it (``CatalogEntryPayload`` JSON)."""
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
        "tags": list(tags),
    }


def _new_from_code(
    position: int,
    name: str,
    code: CodeIdentity,
    name_source: FieldSource = FieldSource.RECEIPT_TEXT,
    entry: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    decision = _decision(position, MatchSource.NEW, name=name)
    decision.update(code=_ref(code), name_source=name_source.value, catalog=entry)
    return decision


def _by_code(
    position: int, source: MatchSource, product_id: int, code: CodeIdentity
) -> dict[str, JsonValue]:
    decision = _decision(position, source, product_id=product_id)
    decision["code"] = _ref(code)
    return decision


def _seed_code(
    harness: Harness, product_id: int, code: CodeIdentity, source: CodeSource
) -> ProductCode:
    value = ProductCode(
        None, product_id, code.scheme, code.store_key, code.code, source, CREATED_AT, CREATED_AT
    )
    return in_unit(harness.uow_factory, lambda uow: uow.product_codes.upsert(value, replace=True))


def _code_rows(harness: Harness) -> tuple[ProductCode, ...]:
    with harness.uow_factory(read_only=True) as uow:
        return uow.product_codes.list()


def _codes(harness: Harness) -> list[CodeRow]:
    return [
        (row.scheme, row.store_key, row.code, row.product_id, row.source)
        for row in _code_rows(harness)
    ]


def _identity(code: CodeIdentity) -> tuple[CodeScheme, str, str]:
    return code.scheme, code.store_key, code.code


def _all_aliases(harness: Harness) -> list[ProductAlias]:
    return [alias for pid in harness.product_ids() for alias in harness.aliases(pid)]


def test_pc_tc_46_a_catalog_decision_links_the_line(harness: Harness, gateway: ToolGateway) -> None:
    choc = _product(harness, "Choc Pistachio Bars")
    _seed_code(harness, choc, C1_CODE, CodeSource.MANUAL)
    codes = _code_rows(harness)
    receipt_id = _coded_receipt(harness, C1, store=COSTCO)

    result = _save(gateway, receipt_id, _by_code(0, MatchSource.CATALOG, choc, C1_CODE))

    assert result["lines"] == [{"position": 0, "product_id": choc, "source": "catalog"}]
    assert result["new_products"] == 0
    assert _linked(harness, receipt_id) == [(choc, MatchSource.CATALOG)]
    assert harness.product_ids() == [choc]
    (price,) = harness.price_history(choc)
    assert price.receipt_id == receipt_id
    assert _code_rows(harness) == codes


@dataclass(frozen=True)
class NewFromCode:
    raw_text: str
    store: tuple[str, str]
    name: str
    name_source: FieldSource
    entry: dict[str, JsonValue] | None
    code: CodeIdentity
    code_source: CodeSource


NEW_FROM_CODE = {
    "store-item-miss": NewFromCode(
        C1, COSTCO, "CHOC PISTACH", FieldSource.RECEIPT_TEXT, None, C1_CODE, CodeSource.RECEIPT
    ),
    "plu": NewFromCode(
        L3, LOBLAWS_STORE, "Test Apple", FieldSource.PLU, _entry("Test Apple"), PLU_3101,
        CodeSource.PLU,
    ),
    "organic-plu": NewFromCode(
        L4, LOBLAWS_STORE, "Organic Test Apple", FieldSource.PLU,
        _entry("Organic Test Apple", ("organic",)), PLU_93101, CodeSource.PLU,
    ),
    "gtin-miss": NewFromCode(
        L1, LOBLAWS_STORE, "PC UNS BABYWIPES", FieldSource.RECEIPT_TEXT, None, L1_CODE,
        CodeSource.RECEIPT,
    ),
}  # fmt: skip


@pytest.mark.parametrize("case", NEW_FROM_CODE.values(), ids=NEW_FROM_CODE.keys())
def test_pc_tc_47_a_new_product_from_a_coded_line_gets_its_code(
    harness: Harness, gateway: ToolGateway, case: NewFromCode
) -> None:
    receipt_id = _coded_receipt(harness, case.raw_text, store=case.store)

    _save(
        gateway,
        receipt_id,
        _new_from_code(0, case.name, case.code, case.name_source, case.entry),
    )

    (created,) = harness.product_ids()
    stored = harness.product(created)
    assert (stored.canonical_name, dict(stored.field_sources)) == (
        case.name,
        {"canonical_name": case.name_source},
    )
    assert _codes(harness) == [(*_identity(case.code), created, case.code_source)]
    assert _linked(harness, receipt_id) == [(created, MatchSource.NEW)]


def test_pc_tc_48_one_product_per_code_on_a_receipt(harness: Harness, gateway: ToolGateway) -> None:
    receipt_id = _coded_receipt(harness, C1, C1, "1362394 CHOC PISTACHIO", store=COSTCO)

    result = _save(
        gateway,
        receipt_id,
        _new_from_code(0, "CHOC PISTACH", C1_CODE),
        _new_from_code(1, "CHOC PISTACH", C1_CODE),
        _new_from_code(2, "CHOC PISTACHIO", C1_CODE),
    )

    (only,) = harness.product_ids()
    assert result["new_products"] == 1
    assert harness.product(only).canonical_name == "CHOC PISTACH"
    assert _codes(harness) == [(*_identity(C1_CODE), only, CodeSource.RECEIPT)]
    assert _linked(harness, receipt_id) == [(only, MatchSource.NEW)] * 3
    assert len(harness.price_history(only)) == 3


def test_pc_tc_48_two_codes_with_one_text_are_two_products(
    harness: Harness, gateway: ToolGateway
) -> None:
    kiwi_code = CodeIdentity(CodeScheme.STORE, STORE_KEY, "1543210")
    receipt_id = _coded_receipt(harness, C1, "1543210 CHOC PISTACH", store=COSTCO)

    _save(
        gateway,
        receipt_id,
        _new_from_code(0, "CHOC PISTACH", C1_CODE),
        _new_from_code(1, "CHOC PISTACH", kiwi_code),
    )

    first, second = harness.product_ids()
    assert _linked(harness, receipt_id) == [(first, MatchSource.NEW), (second, MatchSource.NEW)]
    assert _codes(harness) == [
        (*_identity(C1_CODE), first, CodeSource.RECEIPT),
        (*_identity(kiwi_code), second, CodeSource.RECEIPT),
    ]


def test_pc_tc_48_coded_lines_write_no_text_alias(harness: Harness, gateway: ToolGateway) -> None:
    """TQ25: a coded line is remembered by its code, not its text; a person's alias for the
    same text is kept, and an uncoded line with that text later finds nothing by it."""
    kiwi = _product(harness, "Gold Kiwi")
    _seed_code(
        harness, kiwi, CodeIdentity(CodeScheme.STORE, STORE_KEY, "1543210"), CodeSource.MANUAL
    )
    _alias(harness, kiwi, "gold kiwi", STORE_KEY, AliasSource.USER_CORRECTION)
    aliases = _all_aliases(harness)
    receipt_id = _coded_receipt(harness, C1, C2, store=COSTCO)

    _save(
        gateway,
        receipt_id,
        _new_from_code(0, "CHOC PISTACH", C1_CODE),
        _by_code(
            1, MatchSource.CATALOG, kiwi, CodeIdentity(CodeScheme.STORE, STORE_KEY, "1543210")
        ),
    )

    assert _all_aliases(harness) == aliases
    assert _found(_lookup(gateway, "CHOC PISTACH", STORE_KEY)) == (None, None)


@pytest.mark.parametrize(
    "organic_first", [False, True], ids=["conventional-first", "organic-first"]
)
def test_pc_tc_49_organic_and_conventional_never_share_a_product(
    harness: Harness, gateway: ToolGateway, organic_first: bool
) -> None:
    lines = [
        (L3, "Test Apple", PLU_3101, _entry("Test Apple")),
        (L4, "Organic Test Apple", PLU_93101, _entry("Organic Test Apple", ("organic",))),
    ]
    if organic_first:
        lines.reverse()
    for raw_text, name, code, entry in lines:
        receipt_id = _coded_receipt(harness, raw_text)
        _save(gateway, receipt_id, _new_from_code(0, name, code, FieldSource.PLU, entry))
    first, second = harness.product_ids()
    owner = {row.code: row.product_id for row in _code_rows(harness)}
    assert sorted(owner.values()) == [first, second]

    both = _coded_receipt(harness, L3, L4)
    _save(
        gateway,
        both,
        _by_code(0, MatchSource.CATALOG, owner["3101"], PLU_3101),
        _by_code(1, MatchSource.CATALOG, owner["93101"], PLU_93101),
    )

    assert _linked(harness, both) == [
        (owner["3101"], MatchSource.CATALOG),
        (owner["93101"], MatchSource.CATALOG),
    ]


def test_pc_tc_49_organic_and_conventional_on_one_receipt(
    harness: Harness, gateway: ToolGateway
) -> None:
    receipt_id = _coded_receipt(harness, L3, L4)

    _save(
        gateway,
        receipt_id,
        _new_from_code(0, "Test Apple", PLU_3101, FieldSource.PLU, _entry("Test Apple")),
        _new_from_code(
            1,
            "Organic Test Apple",
            PLU_93101,
            FieldSource.PLU,
            _entry("Organic Test Apple", ("organic",)),
        ),
    )

    apple, organic = harness.product_ids()
    assert _codes(harness) == [
        (CodeScheme.PLU, "", "3101", apple, CodeSource.PLU),
        (CodeScheme.PLU, "", "93101", organic, CodeSource.PLU),
    ]


# PC-TC-50: the decision planned before a person made C1's code name product B.
STALE_DECISIONS = ["new", "catalog", "memory"]


@pytest.mark.parametrize("planned", STALE_DECISIONS)
def test_pc_tc_50_a_stale_coded_decision_follows_the_code(
    harness: Harness, gateway: ToolGateway, planned: str
) -> None:
    """A1, ruling I1: inside the unit the code names B (a person's row), so the line links to
    B as ``catalog`` and the row stays as the person wrote it."""
    planned_for, person_named = _product(harness, "A"), _product(harness, "B")
    _seed_code(harness, person_named, C1_CODE, CodeSource.MANUAL)
    rows = _code_rows(harness)
    receipt_id = _coded_receipt(harness, C1, store=COSTCO)
    decision = {
        "new": _new_from_code(0, "CHOC PISTACH", C1_CODE),
        "catalog": _by_code(0, MatchSource.CATALOG, planned_for, C1_CODE),
        "memory": _by_code(0, MatchSource.MEMORY, planned_for, C1_CODE),
    }[planned]

    with capture_logs() as logs:
        _save(gateway, receipt_id, decision)

    assert _linked(harness, receipt_id) == [(person_named, MatchSource.CATALOG)]
    assert harness.product_ids() == [planned_for, person_named]
    assert _code_rows(harness) == rows
    assert [entry for entry in logs if entry["event"] == "catalog.code_conflict"] == []
    assert len(harness.price_history(person_named)) == 1
    assert harness.price_history(planned_for) == ()


def test_pc_tc_51_an_alias_link_attaches_no_code(harness: Harness, gateway: ToolGateway) -> None:
    """With the catalog off a coded line is matched by its text and carries no code."""
    choc = _product(harness, "Choc")
    receipt_id = _coded_receipt(harness, C1, store=COSTCO)

    _save(gateway, receipt_id, _decision(0, MatchSource.ALIAS, product_id=choc))

    assert _codes(harness) == []


@pytest.mark.parametrize("stale_machine_row", [False, True], ids=["unknown", "machine-row"])
def test_pc_tc_51_a_memory_link_teaches_the_code(
    harness: Harness, gateway: ToolGateway, stale_machine_row: bool
) -> None:
    """A4, ruling I1: a person's correction attaches the code with the person policy, replacing
    a machine's row written before it."""
    corrected = _product(harness, "Corrected")
    if stale_machine_row:
        _seed_code(harness, _product(harness, "CHOC PISTACH"), C1_CODE, CodeSource.RECEIPT)
    receipt_id = _coded_receipt(harness, C1, store=COSTCO)

    _save(gateway, receipt_id, _by_code(0, MatchSource.MEMORY, corrected, C1_CODE))

    assert _linked(harness, receipt_id) == [(corrected, MatchSource.MEMORY)]
    assert _codes(harness) == [(*_identity(C1_CODE), corrected, CodeSource.CORRECTION)]


def test_pc_tc_52_only_a_confirmed_new_product_is_embedded(
    harness: Harness, gateway: ToolGateway
) -> None:
    receipt_id = _coded_receipt(harness, L1, L3)

    _save(
        gateway,
        receipt_id,
        _new_from_code(0, "PC UNS BABYWIPES", L1_CODE),
        _new_from_code(1, "Test Apple", PLU_3101, FieldSource.PLU, _entry("Test Apple")),
    )

    printed, apple = harness.product_ids()
    assert harness.embeddings.requests == [("test apple",)]
    assert harness.has_vector(apple)
    assert not harness.has_vector(printed)


def test_pc_tc_52_printed_text_products_embed_nothing(
    harness: Harness, gateway: ToolGateway
) -> None:
    receipt_id = _coded_receipt(harness, C1, U1, store=COSTCO)

    _save(
        gateway,
        receipt_id,
        _new_from_code(0, "CHOC PISTACH", C1_CODE),
        _decision(1, MatchSource.NEW, name=U1),
    )

    assert len(harness.product_ids()) == 2
    assert harness.embeddings.calls == 0


def test_pc_tc_53_saving_coded_decisions_twice_changes_nothing(
    harness: Harness, gateway: ToolGateway
) -> None:
    receipt_id = _coded_receipt(harness, L1, L3, L1)
    decisions = (
        _new_from_code(0, "PC UNS BABYWIPES", L1_CODE),
        _new_from_code(1, "Test Apple", PLU_3101, FieldSource.PLU, _entry("Test Apple")),
        _new_from_code(2, "PC UNS BABYWIPES", L1_CODE),
    )

    first = _save(gateway, receipt_id, *decisions)
    state = (harness.product_ids(), _code_rows(harness), _linked(harness, receipt_id))
    prices = [harness.price_history(pid) for pid in state[0]]
    again = _save(gateway, receipt_id, *decisions)

    assert again["lines"] == first["lines"]
    assert (harness.product_ids(), _code_rows(harness), _linked(harness, receipt_id)) == state
    assert [harness.price_history(pid) for pid in state[0]] == prices


# --- review round 1 ------------------------------------------------------------------------------

# Codex P1-a: two lines with one code and different decisions, no row for the code yet.
GROUPS = {
    "memory-then-new": ("memory", "new"),
    "new-then-memory": ("new", "memory"),
    "memory-then-catalog": ("memory", "catalog"),
    "catalog-then-memory": ("catalog", "memory"),
}


@pytest.mark.parametrize("planned", GROUPS.values(), ids=GROUPS.keys())
def test_one_code_on_one_receipt_ends_on_the_corrected_product(
    harness: Harness, gateway: ToolGateway, planned: tuple[str, str]
) -> None:
    """A person's correction wins for every line with that code, whatever the line order: no
    line is left on another product, and the code names the corrected one."""
    corrected, other = _product(harness, "Corrected"), _product(harness, "Other")
    receipt_id = _coded_receipt(harness, C1, C1, store=COSTCO)
    decide = {
        "memory": lambda position: _by_code(position, MatchSource.MEMORY, corrected, C1_CODE),
        "new": lambda position: _new_from_code(position, "CHOC PISTACH", C1_CODE),
        "catalog": lambda position: _by_code(position, MatchSource.CATALOG, other, C1_CODE),
    }

    _save(gateway, receipt_id, *(decide[kind](i) for i, kind in enumerate(planned)))

    assert _linked(harness, receipt_id) == [
        (corrected, MatchSource.MEMORY if kind == "memory" else MatchSource.CATALOG)
        for kind in planned
    ]
    assert harness.product_ids() == [corrected, other]
    assert _codes(harness) == [(*_identity(C1_CODE), corrected, CodeSource.CORRECTION)]
    assert len(harness.price_history(corrected)) == 2
    assert harness.price_history(other) == ()


def test_a_saved_new_product_follows_a_persons_remap_on_the_replay(
    harness: Harness, gateway: ToolGateway
) -> None:
    """Codex P1-b: a replay of the same decisions after a person made the code name another
    product links the lines to it (``catalog``) and moves their prices; the product the first
    save created keeps nothing and is not reported as created."""
    receipt_id = _coded_receipt(harness, C1, C1, store=COSTCO)
    decisions = (
        _new_from_code(0, "CHOC PISTACH", C1_CODE),
        _new_from_code(1, "CHOC PISTACH", C1_CODE),
    )
    _save(gateway, receipt_id, *decisions)
    (created,) = harness.product_ids()
    person = _product(harness, "Choc Pistachio Bars")
    _seed_code(harness, person, C1_CODE, CodeSource.MANUAL)

    again = _save(gateway, receipt_id, *decisions)

    assert again["lines"] == [
        {"position": 0, "product_id": person, "source": "catalog"},
        {"position": 1, "product_id": person, "source": "catalog"},
    ]
    assert again["new_products"] == 0
    assert _linked(harness, receipt_id) == [(person, MatchSource.CATALOG)] * 2
    assert len(harness.price_history(person)) == 2
    assert harness.price_history(created) == ()
    assert _codes(harness) == [(*_identity(C1_CODE), person, CodeSource.MANUAL)]


def test_a_catalog_decision_whose_code_was_removed_attaches_it_again(
    harness: Harness, gateway: ToolGateway
) -> None:
    """Fable P3-4: the code row is gone at write time (``catalog remove`` after ``match``); the
    line still links as ``catalog`` and the code names that product again (machine policy)."""
    choc = _product(harness, "Choc Pistachio Bars")
    receipt_id = _coded_receipt(harness, C1, store=COSTCO)

    _save(gateway, receipt_id, _by_code(0, MatchSource.CATALOG, choc, C1_CODE))

    assert _linked(harness, receipt_id) == [(choc, MatchSource.CATALOG)]
    assert _codes(harness) == [(*_identity(C1_CODE), choc, CodeSource.RECEIPT)]


def test_a_decision_for_a_code_the_stored_line_does_not_have_is_refused(
    harness: Harness, gateway: ToolGateway
) -> None:
    """Fable P2-3: the line is stored masked (``644200050535 2`` passes Luhn), so it has no
    code; a decision made from the unmasked text is refused and nothing is written."""
    receipt_id = _coded_receipt(harness, "*********535 2 PK SOAP")
    code = CodeIdentity(CodeScheme.GTIN, "", "0644200050535")

    with pytest.raises(DomainValidationError, match="is not the code of this line"):
        _save(gateway, receipt_id, _new_from_code(0, "2 PK SOAP", code))

    assert harness.product_ids() == []
    assert _linked(harness, receipt_id) == [(None, None)]
    assert _codes(harness) == []


# --- review round 1, scoped re-review --------------------------------------------------------

C1_ALIAS = "choc pistach"


@pytest.mark.parametrize("a_first", [True, False], ids=["a-line-first", "b-line-first"])
@pytest.mark.parametrize("person_row", [True, False], ids=["person-row-b", "newest-correction-b"])
def test_conflicting_corrections_for_one_code_resolve_whatever_the_line_order(
    harness: Harness, gateway: ToolGateway, a_first: bool, person_row: bool
) -> None:
    """Two lines with one code are corrected to A and B. B wins in either line order: a
    person's code row names it (even against A's newer correction), or, without one, its
    correction is the newer. Only the line whose own correction names B keeps MEMORY, the other
    links as CATALOG."""
    a, b = _product(harness, "A"), _product(harness, "B")
    for corrected in (b, a) if person_row else (a, b):
        _remember_link(harness, C1_ALIAS, STORE_KEY, corrected)
    if person_row:
        _seed_code(harness, b, C1_CODE, CodeSource.MANUAL)
    receipt_id = _coded_receipt(harness, C1, C1, store=COSTCO)
    order = (a, b) if a_first else (b, a)

    _save(
        gateway,
        receipt_id,
        *(_by_code(i, MatchSource.MEMORY, pid, C1_CODE) for i, pid in enumerate(order)),
    )

    assert _linked(harness, receipt_id) == [
        (b, MatchSource.MEMORY if pid == b else MatchSource.CATALOG) for pid in order
    ]
    source = CodeSource.MANUAL if person_row else CodeSource.CORRECTION
    assert _codes(harness) == [(*_identity(C1_CODE), b, source)]
    assert harness.price_history(a) == ()


@pytest.mark.parametrize("memory_first", [True, False], ids=["memory-first", "catalog-first"])
def test_a_catalog_choice_for_the_corrected_product_stays_catalog(
    harness: Harness, gateway: ToolGateway, memory_first: bool
) -> None:
    corrected = _product(harness, "Corrected")
    receipt_id = _coded_receipt(harness, C1, C1, store=COSTCO)
    kinds = (MatchSource.MEMORY, MatchSource.CATALOG)
    if not memory_first:
        kinds = kinds[::-1]

    _save(
        gateway,
        receipt_id,
        *(_by_code(i, kind, corrected, C1_CODE) for i, kind in enumerate(kinds)),
    )

    assert _linked(harness, receipt_id) == [(corrected, kind) for kind in kinds]
    assert _codes(harness) == [(*_identity(C1_CODE), corrected, CodeSource.CORRECTION)]


def test_a_replay_after_the_code_was_removed_attaches_it_again(
    harness: Harness, gateway: ToolGateway
) -> None:
    """Commit, the code row removed, the same decisions saved again: the product created for
    the line gets its code back, so a following receipt links to it instead of a duplicate."""
    receipt_id = _coded_receipt(harness, C1, store=COSTCO)
    decision = _new_from_code(0, "CHOC PISTACH", C1_CODE)
    _save(gateway, receipt_id, decision)
    (created,) = harness.product_ids()
    in_unit(
        harness.uow_factory,
        lambda uow: uow.product_codes.remove(CodeScheme.STORE, STORE_KEY, "1362394"),
    )

    _save(gateway, receipt_id, decision)
    following = _coded_receipt(harness, C1, store=COSTCO)
    _save(gateway, following, _new_from_code(0, "CHOC PISTACH", C1_CODE))

    assert _codes(harness) == [(*_identity(C1_CODE), created, CodeSource.RECEIPT)]
    assert _linked(harness, receipt_id) == [(created, MatchSource.NEW)]
    assert _linked(harness, following) == [(created, MatchSource.CATALOG)]
    assert harness.product_ids() == [created]
