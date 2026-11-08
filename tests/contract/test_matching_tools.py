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
from datetime import date
from typing import Any

import pytest

from paper_trail.application.payloads import DraftPayload
from paper_trail.domain.enums import (
    AgentRole,
    AliasSource,
    CorrectionKind,
    MatchSource,
    ProcessingStage,
    ReceiptStatus,
)
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import JsonValue, ProductAlias
from paper_trail.ports.llm import LlmUnavailableError
from paper_trail.ports.tools import ToolGateway, ToolInputError
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from paper_trail.tools.inprocess import InProcessToolGateway
from tests.agent_harness import TOOL_TRANSPORTS, Harness, build_harness, mcp_gateway
from tests.extractions import EGGS_ALIAS, MILK_ALIAS, STORE_KEY, WALMART_KEY
from tests.factories import CREATED_AT, correction, line_item, product, receipt, receipt_draft
from tests.fakes.embeddings import FakeEmbeddings, at_distance, basis
from tests.fakes.llm import ScriptedLlm
from tests.mcp_server import serve_mcp
from tests.storage import in_unit
from tests.support import require_id

MATCHING = AgentRole.MATCHING
SUPERVISOR = AgentRole.SUPERVISOR
TABLE = {EGGS_ALIAS: basis(0), MILK_ALIAS: basis(5)}
PRODUCT_KEYS = {"id", "canonical_name", "brand", "size_value", "size_unit", "category", "tags"}


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


def test_lookup_prefers_the_stores_alias_to_a_global_one(
    harness: Harness, gateway: ToolGateway
) -> None:
    store, anywhere = _product(harness, "at costco"), _product(harness, "anywhere")
    _alias(harness, anywhere, EGGS_ALIAS, None)
    _alias(harness, store, EGGS_ALIAS, STORE_KEY)

    assert _found(_lookup(gateway, EGGS_ALIAS, STORE_KEY)) == (store, "alias")
    assert _found(_lookup(gateway, EGGS_ALIAS, WALMART_KEY)) == (anywhere, "alias")
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


def test_save_matches_links_creates_remembers_embeds_and_prices(
    harness: Harness, gateway: ToolGateway
) -> None:
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
    }
    assert _linked(harness, receipt_id) == [(eggs, MatchSource.ALIAS), (milk, MatchSource.NEW)]
    assert harness.product(milk).canonical_name == "Kirkland 2% Milk 4 L"
    assert harness.find_by_alias(EGGS_ALIAS, STORE_KEY) == harness.product(eggs)
    assert harness.find_by_alias(MILK_ALIAS, STORE_KEY) == harness.product(milk)
    assert {alias.source for alias in harness.aliases(milk)} == {AliasSource.MATCHED}
    assert harness.embeddings.requests == [(MILK_ALIAS,)]
    with harness.uow_factory(read_only=True) as uow:
        (nearest,) = uow.vectors.nearest(TABLE[MILK_ALIAS], k=1)
    assert nearest.product_id == milk
    assert nearest.distance == pytest.approx(0.0, abs=1e-6)
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
    receipt_id = _stored_receipt(harness)

    with pytest.raises(NotFoundError):
        _save(gateway, receipt_id, _decision(1, MatchSource.NEW, name="Milk"), decision)

    assert harness.product_ids() == [1]
    assert _linked(harness, receipt_id) == [(None, None), (None, None)]


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
    ],
    ids=["neither", "both", "new-with-product", "named-not-new"],
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
    """AC-C-02: all links are written in one unit of work; a failure leaves no partial links."""
    down = FakeEmbeddings(error=LlmUnavailableError("cannot reach llm-host.test:11434"))
    harness = build_harness(ScriptedLlm(), uow_factory=uow_factory, embeddings=down)
    eggs = _product(harness, "eggs")
    receipt_id = _stored_receipt(harness)

    with _gateway(harness, transport_name) as gateway, pytest.raises(LlmUnavailableError):
        _save(
            gateway,
            receipt_id,
            _decision(0, MatchSource.ALIAS, product_id=eggs),
            _decision(1, MatchSource.NEW, name="Milk"),
        )

    assert harness.product_ids() == [eggs]
    assert _linked(harness, receipt_id) == [(None, None), (None, None)]
    assert harness.aliases(eggs) == ()
    assert harness.price_history(eggs) == ()


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
