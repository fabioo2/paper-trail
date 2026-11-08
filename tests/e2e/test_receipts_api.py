"""Receipts and categories over HTTP through the fully wired app (S1-AC-22, S1-AC-23, S1-AC-25).

Receipts are recorded with the container's own RecordReceipt use case, the same object the
CLI import uses (``Container.record_receipt``, PLAN 3.6).
"""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from paper_trail.application.receipts import GetReceipt, ListReceipts, RecordReceipt
from paper_trail.bootstrap import Container
from paper_trail.config import Settings
from paper_trail.domain.enums import Category, LineKind
from paper_trail.domain.models import Receipt
from tests.factories import cad, line_item, receipt_draft
from tests.support import require_id

PROBLEM_JSON = "application/problem+json"
SUMMARY_KEYS = {"id", "status", "store_name", "purchased_on", "total", "item_count", "created_at"}
DETAIL_KEYS = SUMMARY_KEYS | {
    "stage",
    "subtotal",
    "tax",
    "payment_last4",
    "has_image",
    "items",
    "arithmetic",
}
LINE_KEYS = {
    "id",
    "position",
    "kind",
    "raw_text",
    "description",
    "quantity",
    "unit_price",
    "line_total",
    "product_id",
    "category",
}
ISSUE_KEYS = {"code", "message", "expected", "actual", "position"}


def _record(container: Container, **overrides: object) -> Receipt:
    return container.record_receipt(receipt_draft(**overrides)).receipt


def _problem(client: TestClient, url: str, status: int) -> dict[str, object]:
    response = client.get(url)
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body: dict[str, object] = response.json()
    assert body["status"] == status
    return body


@pytest.fixture
def weighed(migrated_container: Container) -> Receipt:
    """A completed receipt with a weighed line and a discount (has an image)."""
    items = (
        line_item(position=0, raw_text="BANANAS", description="Bananas", quantity=Decimal("0.452"),
                  unit_price=cad(1099), line_total=cad(497), category=Category.PRODUCE),
        line_item(position=1, kind=LineKind.DISCOUNT, raw_text="TPD", description="Savings",
                  unit_price=None, line_total=cad(-100)),
    )  # fmt: skip
    return _record(
        migrated_container,
        items=items,
        subtotal=cad(397),
        tax=None,
        total=cad(397),
        image_path="2026/11/bananas.jpg",
    )


def test_s1_ac_22_list_shape(migrated_client: TestClient, weighed: Receipt) -> None:
    """S1-AC-22: ReceiptPageOut { items: ReceiptSummaryOut[], next_cursor }."""
    response = migrated_client.get("/api/receipts")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "next_cursor"}
    assert body["next_cursor"] is None
    (summary,) = body["items"]
    assert set(summary) == SUMMARY_KEYS
    assert summary["id"] == weighed.id
    assert summary["status"] == "completed"
    assert summary["store_name"] == "COSTCO WHOLESALE #1234"
    assert summary["purchased_on"] == "2026-11-01"
    assert summary["total"] == {"amount_cents": 397, "currency": "CAD"}
    assert summary["item_count"] == 2
    created = datetime.fromisoformat(summary["created_at"])
    assert created.utcoffset() is not None
    assert created == weighed.created_at


def test_s1_ac_22_detail_shape(migrated_client: TestClient, weighed: Receipt) -> None:
    """S1-AC-22: ReceiptDetailOut; money is {amount_cents, currency}; quantity a decimal string."""
    response = migrated_client.get(f"/api/receipts/{weighed.id}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == DETAIL_KEYS
    assert "ocr_text" not in body, "ocr_text is exposed from Stage 2 only"
    assert body["stage"] == "done"
    assert body["subtotal"] == {"amount_cents": 397, "currency": "CAD"}
    assert body["tax"] is None
    assert body["payment_last4"] == "1111"
    assert body["has_image"] is True
    bananas, savings = body["items"]
    assert set(bananas) == LINE_KEYS
    assert bananas == {
        "id": weighed.items[0].id,
        "position": 0,
        "kind": "item",
        "raw_text": "BANANAS",
        "description": "Bananas",
        "quantity": "0.452",
        "unit_price": {"amount_cents": 1099, "currency": "CAD"},
        "line_total": {"amount_cents": 497, "currency": "CAD"},
        "product_id": None,
        "category": "produce",
    }
    assert savings["kind"] == "discount"
    assert savings["unit_price"] is None
    assert savings["line_total"] == {"amount_cents": -100, "currency": "CAD"}
    assert body["arithmetic"] == {"ok": True, "issues": []}


def test_s1_ac_22_detail_without_image_or_date(
    migrated_client: TestClient, migrated_container: Container
) -> None:
    """S1-AC-22: has_image is false without an image; nullable fields are null."""
    saved = _record(migrated_container, purchased_on=None, payment_last4=None)

    body = migrated_client.get(f"/api/receipts/{saved.id}").json()

    assert body["has_image"] is False
    assert body["purchased_on"] is None
    assert body["payment_last4"] is None


def test_s1_ac_22_arithmetic_reflects_the_current_data(
    migrated_client: TestClient, migrated_container: Container
) -> None:
    """S1-AC-22: issues are listed while the data is wrong and gone once it is fixed."""
    eggs, milk = receipt_draft().items
    saved = _record(migrated_container, items=(eggs, replace(milk, line_total=cad(685))))
    url = f"/api/receipts/{saved.id}"

    before = migrated_client.get(url).json()
    assert before["status"] == "needs_review"
    assert before["arithmetic"]["ok"] is False
    issues = before["arithmetic"]["issues"]
    assert {issue["code"] for issue in issues} >= {"line_math", "items_vs_subtotal"}
    for issue in issues:
        assert set(issue) == ISSUE_KEYS
    line_math = next(issue for issue in issues if issue["code"] == "line_math")
    assert line_math["position"] == 1
    assert line_math["expected"] == {"amount_cents": 658, "currency": "CAD"}
    assert line_math["actual"] == {"amount_cents": 685, "currency": "CAD"}
    assert "line 2" in line_math["message"]

    with migrated_container.engine.begin() as connection:
        connection.execute(
            text("UPDATE line_items SET line_total_cents = 658 WHERE id = :id"),
            {"id": saved.items[1].id},
        )

    after = migrated_client.get(url).json()
    assert after["arithmetic"] == {"ok": True, "issues": []}


def test_s1_ac_22_pages_through_every_receipt_once(
    migrated_client: TestClient, migrated_container: Container
) -> None:
    """S1-AC-22: limit and cursor page through all receipts newest first."""
    ids = [require_id(_record(migrated_container).id) for _ in range(5)]

    seen: list[int] = []
    url = "/api/receipts?limit=2"
    while True:
        body = migrated_client.get(url).json()
        seen += [summary["id"] for summary in body["items"]]
        if body["next_cursor"] is None:
            break
        url = f"/api/receipts?limit=2&cursor={body['next_cursor']}"

    assert seen == sorted(ids, reverse=True)


def test_s1_ac_22_filters_by_status_and_normalized_store(
    migrated_client: TestClient, migrated_container: Container
) -> None:
    """S1-AC-22 / PLAN 3.6: ?status= filters by status; ?store= is normalized first."""
    costco = _record(migrated_container)
    walmart = _record(migrated_container, store_name="WALMART #3150")
    broken = _record(migrated_container, store_name="Walmart", total=cad(1))

    def ids(query: str) -> list[int]:
        response = migrated_client.get(f"/api/receipts?{query}")
        assert response.status_code == 200, response.text
        return [summary["id"] for summary in response.json()["items"]]

    assert ids("status=needs_review") == [broken.id]
    assert ids("store=Walmart%20Supercentre") == [broken.id, walmart.id]
    assert ids("store=COSTCO%20WHOLESALE%20%23999") == [costco.id]
    assert ids("store=walmart&status=completed") == [walmart.id]


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=101", "limit=-5", "limit=ten", "cursor=garbage", "cursor=%21%21",
     "status=lost"],
)  # fmt: skip
def test_s1_ac_22_bad_query_is_a_422_problem(
    migrated_client: TestClient, weighed: Receipt, query: str
) -> None:
    """S1-AC-22: limit outside 1..100, a bad cursor or an unknown status -> 422 problem+json."""
    body = _problem(migrated_client, f"/api/receipts?{query}", 422)

    assert body["type"] == "/problems/validation"


def test_s1_ac_22_unknown_receipt_is_a_404_problem(migrated_client: TestClient) -> None:
    """S1-AC-22: an unknown id -> 404 problem+json."""
    body = _problem(migrated_client, "/api/receipts/424242", 404)

    assert body["type"] == "/problems/not-found"
    assert body["instance"] == "/api/receipts/424242"


def test_s1_ac_22_non_integer_id_is_a_422_problem(migrated_client: TestClient) -> None:
    """S1-AC-22: the path parameter is an int."""
    _problem(migrated_client, "/api/receipts/abc", 422)


def test_s1_ac_22_empty_list(migrated_client: TestClient) -> None:
    """S1-AC-22: no receipts -> an empty page."""
    assert migrated_client.get("/api/receipts").json() == {"items": [], "next_cursor": None}


def test_s1_ac_22_container_wires_the_receipt_use_cases(
    container: Container, settings: Settings
) -> None:
    """PLAN 3.6: the container exposes the use cases; building it still opens no database."""
    assert isinstance(container.record_receipt, RecordReceipt)
    assert isinstance(container.get_receipt, GetReceipt)
    assert isinstance(container.list_receipts, ListReceipts)
    assert callable(container.uow_factory)
    assert not settings.database_path.exists()


# --- categories (S1-AC-23) -------------------------------------------------------------------


def test_s1_ac_23_categories_lists_every_category_once(migrated_client: TestClient) -> None:
    """S1-AC-23: every Category value exactly once, in enum order, each with a non-empty label."""
    response = migrated_client.get("/api/categories")

    assert response.status_code == 200
    body = response.json()
    assert [entry["value"] for entry in body] == [category.value for category in Category]
    for entry in body:
        assert set(entry) == {"value", "label"}
        assert entry["label"].strip()
    assert len({entry["label"] for entry in body}) == len(body), "labels are distinct"


def test_s1_ac_23_categories_need_no_database(client: TestClient) -> None:
    """S1-AC-23: categories are static; they answer even before the database is migrated."""
    assert client.get("/api/categories").status_code == 200


# --- OpenAPI (S1-AC-25) ----------------------------------------------------------------------


def test_s1_ac_25_operations_are_in_the_contract(app: FastAPI) -> None:
    """S1-AC-25: listReceipts, getReceipt and listCategories with their problem responses."""
    paths = app.openapi()["paths"]

    list_op = paths["/api/receipts"]["get"]
    get_op = paths["/api/receipts/{receipt_id}"]["get"]
    categories_op = paths["/api/categories"]["get"]
    assert (list_op["operationId"], get_op["operationId"], categories_op["operationId"]) == (
        "listReceipts",
        "getReceipt",
        "listCategories",
    )
    assert PROBLEM_JSON in list_op["responses"]["422"]["content"]
    assert PROBLEM_JSON in get_op["responses"]["404"]["content"]
    for operation in (list_op, get_op, categories_op):
        assert PROBLEM_JSON in operation["responses"]["500"]["content"]


def test_s1_ac_25_schemas_are_named_for_the_client(app: FastAPI) -> None:
    """S1-AC-25 / PLAN 3.7: the TS client gets these schema names."""
    schemas = app.openapi()["components"]["schemas"]

    for name in (
        "MoneyOut",
        "ReceiptSummaryOut",
        "LineItemOut",
        "ArithmeticIssueOut",
        "ArithmeticReportOut",
        "ReceiptDetailOut",
        "ReceiptPageOut",
        "CategoryOut",
    ):
        assert name in schemas, name
