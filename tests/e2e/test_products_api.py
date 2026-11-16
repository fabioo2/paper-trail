"""Products over HTTP: the list, one product with where each field came from
(claude-extraction plan D4; CE-TC-251), and a person's edit or confirmation."""

import pytest
from fastapi.testclient import TestClient

from paper_trail.bootstrap import Container
from paper_trail.domain.enums import FieldSource, MatchSource
from tests.factories import PURCHASED_ON, STORE_NAME, item_line, product
from tests.storage import in_unit, seed_purchase
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


def _stored(container: Container, **overrides: object) -> int:
    stored = product(**overrides)
    return require_id(in_unit(container.uow_factory, lambda uow: uow.products.add(stored)).id)


def test_the_list_shows_each_bought_product_with_its_last_purchase(
    migrated_container: Container, migrated_client: TestClient
) -> None:
    product_id = _stored(migrated_container, field_sources={"canonical_name": FieldSource.LLM})
    _stored(migrated_container, canonical_name="Never bought")
    seed_purchase(
        migrated_container.uow_factory,
        [item_line(0, "KS ORG EGGS", 899, product_id=product_id, match_source=MatchSource.NEW)],
    )

    response = migrated_client.get("/api/products")

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "id": product_id,
            "canonical_name": "Kirkland Signature Organic Eggs",
            "brand": "Kirkland Signature",
            "size_value": "24",
            "size_unit": "each",
            "category": "dairy_eggs",
            "tags": ["organic", "free-run"],
            "field_sources": {"canonical_name": "llm"},
            "confirmed": False,
            "times_bought": 1,
            "last_purchase": {
                "purchased_on": PURCHASED_ON.isoformat(),
                "store_name": STORE_NAME,
                "paid": {"amount_cents": 899, "currency": "CAD"},
            },
        }
    ]


def test_an_edit_stamps_the_sent_fields_person_and_keeps_the_others(
    migrated_container: Container, migrated_client: TestClient
) -> None:
    guessed = dict.fromkeys(("canonical_name", "brand"), FieldSource.LLM)
    product_id = _stored(migrated_container, field_sources=guessed)

    response = migrated_client.patch(
        f"/api/products/{product_id}", json={"canonical_name": "Large Eggs", "size": "12 each"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["canonical_name"], body["brand"], body["size_value"], body["size_unit"]) == (
        "Large Eggs",
        "Kirkland Signature",
        "12",
        "each",
    )
    assert body["field_sources"] == {"canonical_name": "person", "brand": "llm", "size": "person"}
    assert body["confirmed"] is True
    assert migrated_client.get(f"/api/products/{product_id}").json() == body


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"size": "a dozen"}, "size 'a dozen': give a positive number"),
        ({"canonical_name": "x" * 121}, "The request is invalid."),
        ({"tags": ["x" * 31]}, "The request is invalid."),
        ({"tags": ["sweet, salty"]}, "a tag has no comma"),
        ({"category": "toys"}, "The request is invalid."),
        ({"unknown": "field"}, "The request is invalid."),
    ],
)
def test_an_invalid_edit_is_a_422_problem(
    migrated_container: Container, migrated_client: TestClient, body: object, detail: str
) -> None:
    product_id = _stored(migrated_container)

    response = migrated_client.patch(f"/api/products/{product_id}", json=body)

    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["detail"].startswith(detail)


def test_confirm_makes_the_guessed_values_the_persons(
    migrated_container: Container, migrated_client: TestClient
) -> None:
    product_id = _stored(
        migrated_container,
        tags=(),
        field_sources={"canonical_name": FieldSource.LLM, "category": FieldSource.LLM},
    )

    response = migrated_client.post(f"/api/products/{product_id}/confirm")

    assert response.status_code == 200, response.text
    assert response.json()["field_sources"] == {
        "canonical_name": "person",
        "brand": "person",
        "size": "person",
        "category": "person",
    }
    assert response.json()["confirmed"] is True


@pytest.mark.parametrize(
    ("method", "url"),
    [("PATCH", "/api/products/424242"), ("POST", "/api/products/424242/confirm")],
)
def test_editing_an_unknown_product_is_a_404_problem(
    migrated_client: TestClient, method: str, url: str
) -> None:
    response = migrated_client.request(method, url, json={"canonical_name": "Eggs"})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
