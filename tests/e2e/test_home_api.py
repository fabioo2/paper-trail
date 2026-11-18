"""Home's month summary and the header search over HTTP: the wire shape, the default month and
the 422 problems for a malformed month or a query of the wrong length. What is counted and
found is pinned in ``tests/integration/test_summary_sqlite.py`` and ``test_search_sqlite.py``."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from paper_trail.bootstrap import Container
from paper_trail.domain.enums import MatchSource
from tests.factories import PURCHASED_ON, STORE_NAME, discount_line, item_line, product
from tests.storage import in_unit, seed_purchase
from tests.support import require_id


def _seed(container: Container) -> tuple[int, int]:
    eggs = in_unit(container.uow_factory, lambda uow: uow.products.add(product()))
    receipt = seed_purchase(
        container.uow_factory,
        [
            item_line(0, "KS ORG EGGS", 1699, product_id=require_id(eggs.id),
                      match_source=MatchSource.NEW),
            discount_line(1, "TPD", -400, 0),
        ],
    )  # fmt: skip
    return require_id(eggs.id), require_id(receipt.id)


def test_a_months_summary(migrated_container: Container, migrated_client: TestClient) -> None:
    _, receipt_id = _seed(migrated_container)

    response = migrated_client.get("/api/summary", params={"month": PURCHASED_ON.strftime("%Y-%m")})

    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["id"] for r in body.pop("recent")] == [receipt_id]
    assert body == {
        "month": "2026-11",
        "spent": [{"amount_cents": 1635, "currency": "CAD"}],
        "saved": [{"amount_cents": 400, "currency": "CAD"}],
        "trips": 1,
        "weeks": [
            {"start": "2026-11-01", "end": "2026-11-07",
             "spent": [{"amount_cents": 1635, "currency": "CAD"}]},
            {"start": "2026-11-08", "end": "2026-11-14", "spent": []},
            {"start": "2026-11-15", "end": "2026-11-21", "spent": []},
            {"start": "2026-11-22", "end": "2026-11-30", "spent": []},
        ],
        "stores": [{"store_name": STORE_NAME, "spent": {"amount_cents": 1635, "currency": "CAD"}}],
        "open_reviews": 0,
        "unconfirmed_products": 1,
        "processing": 0,
    }  # fmt: skip


def test_the_summary_defaults_to_the_servers_month(migrated_client: TestClient) -> None:
    response = migrated_client.get("/api/summary")

    assert response.status_code == 200, response.text
    assert response.json()["month"] == datetime.now(UTC).strftime("%Y-%m")


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-1", "26-11", "0999-01", "nov", ""])
def test_a_malformed_month_is_a_422_problem(migrated_client: TestClient, month: str) -> None:
    response = migrated_client.get("/api/summary", params={"month": month})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_a_search_finds_products_and_receipts(
    migrated_container: Container, migrated_client: TestClient
) -> None:
    eggs_id, receipt_id = _seed(migrated_container)

    response = migrated_client.get("/api/search", params={"q": "eggs"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(p["id"], p["times_bought"]) for p in body["products"]] == [(eggs_id, 1)]
    [found] = body["receipts"]
    assert found["id"] == receipt_id
    assert found["lines"] == ["Ks Org Eggs"]
    assert found["savings"] == {"amount_cents": 400, "currency": "CAD"}


@pytest.mark.parametrize("q", ["", "e", "x" * 101])
def test_a_query_too_short_or_too_long_is_a_422_problem(
    migrated_client: TestClient, q: str
) -> None:
    response = migrated_client.get("/api/search", params={"q": q})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_a_search_without_a_query_is_a_422_problem(migrated_client: TestClient) -> None:
    assert migrated_client.get("/api/search").status_code == 422
