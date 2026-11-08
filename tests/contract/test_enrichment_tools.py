"""``save_enrichment``, the supervisor's enrichment write, on every transport and storage (PLAN 6,
PLAN 7, AC-D-02, S4-AC-05).

Called through the in-process gateway and over MCP, each over the fake and the SQLite unit of
work (real sqlite-vec index and FTS triggers). Embeddings are ``FakeEmbeddings``; nothing
contacts a model.

Pinned (docs/tests/agent-core-D.md):
- ``save_enrichment {product_id: int, alias: str, store_key: str | null, enrichment:
  {canonical_name, brand, size_value: str | null, size_unit, category, tags}}`` ->
  ``{product: ProductOut, category_source: "model" | "memory"}``. ``enrichment`` is the model's
  answer without its confidence (no other field is accepted: ``ToolInputError``). ``alias`` is
  the line the product was created from; the tool normalizes it.
- Category memory overrides the model: the newest CATEGORY correction for exactly (alias,
  store_key) names the stored category (``category_source`` ``memory``); else the model's
  (``model``). Other corrections, and CATEGORY corrections of another store or alias, do not
  count.
- In one unit of work: the product's name (card numbers masked), brand, size, category and tags
  are replaced (``ProductRepository.update``), and its vector is replaced by the embedding of
  ``normalize_alias(" ".join([canonical_name, *tags]))``, computed before the write in one call
  with one text. Its aliases and prices are untouched.
- Unknown product: NotFoundError and nothing written. A failed embedding call writes nothing and
  keeps its class (``LlmUnavailableError``) over MCP too. The same call twice leaves the same
  product and one vector (idempotent, for a resumed run).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from paper_trail.domain.enums import AgentRole, Category, CorrectionKind, Unit
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import JsonValue, Product
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.ports.llm import LlmUnavailableError
from paper_trail.ports.tools import ToolGateway, ToolInputError
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from paper_trail.tools.inprocess import InProcessToolGateway
from tests.agent_harness import TOOL_TRANSPORTS, Harness, build_harness, mcp_gateway
from tests.extractions import EGGS_ALIAS, STORE_KEY, WALMART_KEY, enrichment
from tests.factories import MASKED_TEST_PAN, TEST_PAN, at, correction, product, product_alias
from tests.fakes.embeddings import FakeEmbeddings, basis
from tests.fakes.llm import ScriptedLlm
from tests.mcp_server import serve_mcp
from tests.storage import in_unit
from tests.support import require_id

SUPERVISOR = AgentRole.SUPERVISOR
NAME = "Kirkland Signature Organic Eggs 24 ct"
TAGS = ("eggs", "organic", "breakfast")
ENRICHED_TEXT = normalize_alias(" ".join([NAME, *TAGS]))


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
    return build_harness(ScriptedLlm(), uow_factory=uow_factory, embeddings=FakeEmbeddings())


@pytest.fixture
def gateway(harness: Harness, transport_name: str) -> Iterator[ToolGateway]:
    with _gateway(harness, transport_name) as gateway:
        yield gateway


def _bare_product(harness: Harness) -> int:
    """A product as ``save_matches`` creates it: a name, an alias at the store, a vector."""
    bare = product(
        canonical_name="Kirkland Organic Eggs 24 ct",
        brand=None,
        size_value=None,
        size_unit=None,
        category=None,
        tags=(),
    )

    def add(uow: UnitOfWork) -> int:
        product_id = require_id(uow.products.add(bare).id)
        uow.products.add_alias(product_alias(product_id, alias=EGGS_ALIAS, store_key=STORE_KEY))
        uow.vectors.upsert(product_id, basis(0))
        return product_id

    return in_unit(harness.uow_factory, add)


def _remember_category(
    harness: Harness, category: Category, *, store_key: str | None = STORE_KEY, minutes: int = 0
) -> None:
    value = correction(
        kind=CorrectionKind.CATEGORY,
        subject=EGGS_ALIAS,
        old_value=None,
        new_value=category.value,
        store_key=store_key,
        created_at=at(minutes),
    )
    in_unit(harness.uow_factory, lambda uow: uow.corrections.add(value))


def _without_confidence(answer: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in answer.items() if key != "confidence"}


def _payload(product_id: int, /, **overrides: Any) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "product_id": product_id,
        "alias": "KS ORG EGGS 24CT",
        "store_key": STORE_KEY,
        "enrichment": _without_confidence(enrichment(NAME, tags=TAGS)),
    }
    payload.update(overrides)
    return payload


def _stored(harness: Harness, product_id: int) -> Product:
    return harness.product(product_id)


def _nearest(harness: Harness, vector: tuple[float, ...]) -> tuple[int, float]:
    with harness.uow_factory(read_only=True) as uow:
        (hit,) = uow.vectors.nearest(vector, k=1)
    return hit.product_id, round(hit.distance, 6)


def test_save_enrichment_replaces_the_products_fields(
    harness: Harness, gateway: ToolGateway
) -> None:
    product_id = _bare_product(harness)
    created_at = _stored(harness, product_id).created_at

    result = gateway.call(SUPERVISOR, "save_enrichment", _payload(product_id))

    stored = _stored(harness, product_id)
    assert (stored.canonical_name, stored.brand) == (NAME, "Kirkland Signature")
    assert (str(stored.size_value), stored.size_unit) == ("24", Unit.EACH)
    assert stored.category is Category.DAIRY_EGGS
    assert stored.tags == TAGS
    assert stored.created_at == created_at
    assert result["category_source"] == "model"
    output = result["product"]
    assert isinstance(output, dict)
    assert (output["id"], output["canonical_name"], output["category"]) == (
        product_id,
        NAME,
        "dairy_eggs",
    )
    assert output["tags"] == list(TAGS)
    assert [alias.alias for alias in harness.aliases(product_id)] == [EGGS_ALIAS]


def test_save_enrichment_replaces_the_vector_with_the_enriched_text(
    harness: Harness, gateway: ToolGateway
) -> None:
    product_id = _bare_product(harness)

    gateway.call(SUPERVISOR, "save_enrichment", _payload(product_id))

    assert harness.embeddings.requests == [(ENRICHED_TEXT,)]
    assert _nearest(harness, harness.embeddings.vector(ENRICHED_TEXT)) == (product_id, 0.0)


def test_s4_ac_05_a_category_correction_for_the_alias_overrides_the_model(
    harness: Harness, gateway: ToolGateway
) -> None:
    product_id = _bare_product(harness)
    _remember_category(harness, Category.PANTRY, minutes=0)
    _remember_category(harness, Category.BAKERY, minutes=5)  # the newest wins

    result = gateway.call(SUPERVISOR, "save_enrichment", _payload(product_id))

    assert _stored(harness, product_id).category is Category.BAKERY
    assert result["category_source"] == "memory"
    output = result["product"]
    assert isinstance(output, dict)
    assert output["category"] == "bakery"


@pytest.mark.parametrize(
    "other",
    [
        {"store_key": WALMART_KEY},
        {"subject": "ks sourdough"},
        {"kind": CorrectionKind.PRODUCT_LINK, "new_value": "1"},
    ],
    ids=["another-store", "another-alias", "not-a-category"],
)
def test_only_a_category_correction_for_this_alias_and_store_counts(
    harness: Harness, gateway: ToolGateway, other: dict[str, Any]
) -> None:
    product_id = _bare_product(harness)
    unrelated = correction(
        **{
            "kind": CorrectionKind.CATEGORY,
            "subject": EGGS_ALIAS,
            "old_value": None,
            "new_value": Category.PANTRY.value,
            **other,
        }
    )
    in_unit(harness.uow_factory, lambda uow: uow.corrections.add(unrelated))

    result = gateway.call(SUPERVISOR, "save_enrichment", _payload(product_id))

    assert _stored(harness, product_id).category is Category.DAIRY_EGGS
    assert result["category_source"] == "model"


def test_the_name_is_masked_like_all_free_text(harness: Harness, gateway: ToolGateway) -> None:
    product_id = _bare_product(harness)
    answer = _without_confidence(enrichment(f"Gift card {TEST_PAN}", tags=()))

    gateway.call(SUPERVISOR, "save_enrichment", _payload(product_id, enrichment=answer))

    assert _stored(harness, product_id).canonical_name == f"Gift card {MASKED_TEST_PAN}"
    assert TEST_PAN.replace(" ", "") not in " ".join(harness.embeddings.texts).replace(" ", "")


def test_save_enrichment_is_idempotent(harness: Harness, gateway: ToolGateway) -> None:
    product_id = _bare_product(harness)

    first = gateway.call(SUPERVISOR, "save_enrichment", _payload(product_id))
    again = gateway.call(SUPERVISOR, "save_enrichment", _payload(product_id))

    assert again == first
    with harness.uow_factory(read_only=True) as uow:
        hits = uow.vectors.nearest(harness.embeddings.vector(ENRICHED_TEXT), k=10)
    assert [hit.product_id for hit in hits] == [product_id]


def test_an_unknown_product_is_not_found_and_nothing_is_written(
    harness: Harness, gateway: ToolGateway
) -> None:
    product_id = _bare_product(harness)
    before = _stored(harness, product_id)

    with pytest.raises(NotFoundError):
        gateway.call(SUPERVISOR, "save_enrichment", _payload(product_id + 100))

    assert _stored(harness, product_id) == before
    assert _nearest(harness, basis(0)) == (product_id, 0.0)


def test_a_failed_embedding_writes_nothing_and_keeps_its_class(
    uow_factory: UnitOfWorkFactory, transport_name: str
) -> None:
    harness = build_harness(
        ScriptedLlm(),
        uow_factory=uow_factory,
        embeddings=FakeEmbeddings(error=LlmUnavailableError("cannot reach llm-host.test:11434")),
    )
    product_id = _bare_product(harness)
    before = _stored(harness, product_id)

    with (
        _gateway(harness, transport_name) as gateway,
        pytest.raises(LlmUnavailableError, match=r"llm-host\.test:11434"),
    ):
        gateway.call(SUPERVISOR, "save_enrichment", _payload(product_id))

    assert _stored(harness, product_id) == before
    assert _nearest(harness, basis(0)) == (product_id, 0.0)


@pytest.mark.parametrize(
    "change",
    [
        {"enrichment": enrichment()},
        {"enrichment": _without_confidence(enrichment(category="groceries"))},
        {"product_id": "1"},
        {"alias": None},
    ],
    ids=["with-confidence", "invented-category", "string-id", "no-alias"],
)
def test_invalid_input_is_refused(
    harness: Harness, gateway: ToolGateway, change: dict[str, Any]
) -> None:
    product_id = _bare_product(harness)

    with pytest.raises(ToolInputError):
        gateway.call(SUPERVISOR, "save_enrichment", _payload(product_id, **change))

    assert _stored(harness, product_id).category is None
