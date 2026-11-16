"""A product over HTTP with where each field came from (claude-extraction plan D4; CE-TC-251)."""

from fastapi.testclient import TestClient

from paper_trail.bootstrap import Container
from paper_trail.domain.enums import FieldSource
from tests.factories import product
from tests.storage import in_unit
from tests.support import require_id


def test_a_product_shows_its_field_sources(
    migrated_container: Container, migrated_client: TestClient
) -> None:
    stored = product(
        size_value=None,
        size_unit=None,
        field_sources={"canonical_name": FieldSource.LLM, "category": FieldSource.RECEIPT_TEXT},
    )
    product_id = require_id(
        in_unit(migrated_container.uow_factory, lambda uow: uow.products.add(stored)).id
    )

    response = migrated_client.get(f"/api/products/{product_id}")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": product_id,
        "canonical_name": stored.canonical_name,
        "brand": stored.brand,
        "size_value": None,
        "size_unit": None,
        "category": "dairy_eggs",
        "tags": list(stored.tags),
        "field_sources": {"canonical_name": "llm", "category": "receipt_text"},
        "confirmed": False,
    }


def test_an_unknown_product_is_a_404_problem(migrated_client: TestClient) -> None:
    response = migrated_client.get("/api/products/424242")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
