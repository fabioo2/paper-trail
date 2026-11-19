"""Reviews over HTTP through the fully wired app: ``review_services`` reads the parked thread's
checkpoint from the database file, ``POST .../resolve`` queues the answer, and the worker's job
(``ProcessReceiptJobs``) saves it. The graph that parks the receipt runs from fakes
(``tests/agent_harness``) on the same file, standing for the worker's earlier run.

Pinned: the list shows each open review with its issues and a summary of the reading; one
review shows the full reading and its photo's URL; an inconsistent answer is a 422 problem of
type /problems/inconsistent-receipt with one ``errors`` entry per issue, unless forced with a
reason; a corrected reading is a 202 with the receipt PROCESSING/QUEUED, left out of the list,
and a second answer a 409; the worker then completes the receipt and resolves the review;
unknown reviews are 404 problems; without review services the endpoints are 503 problems; a
receipt's photo is served as JPEG.
"""

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from paper_trail.adapters.sqlite.checkpoints import sqlite_checkpointer
from paper_trail.application.jobs import ProcessReceiptJobs
from paper_trail.bootstrap import Container, image_store
from paper_trail.bootstrap.agents import review_services
from paper_trail.config import Settings
from paper_trail.domain.enums import JobKind
from paper_trail.domain.models import ReceiptDraft
from paper_trail.interfaces.web.app import create_app
from tests.agent_harness import Harness, agent_settings, build_harness
from tests.extractions import STORE_NAME, inconsistent_answer
from tests.factories import cad, receipt, receipt_draft
from tests.fakes.reader import ScriptedReader
from tests.images import png_bytes
from tests.storage import in_unit
from tests.support import require_id

PROBLEM_JSON = "application/problem+json"


@dataclass
class Parked:
    client: TestClient
    harness: Harness
    receipt_id: int
    review_id: int

    @property
    def url(self) -> str:
        return f"/api/reviews/{self.review_id}"

    def run_worker(self) -> bool:
        return ProcessReceiptJobs(
            self.harness.uow_factory,
            self.harness.process,
            self.harness.tracer,
            worker_id="worker-1",
            lease=timedelta(minutes=5),
            resolve_review=self.harness.resolve,
        )()

    def receipt_state(self) -> tuple[str, str]:
        body = self.client.get(f"/api/receipts/{self.receipt_id}").json()
        return body["status"], body["stage"]


@pytest.fixture
def parked(migrated_settings: Settings, migrated_container: Container) -> Iterator[Parked]:
    """Review of a receipt whose one reading came back 9 cents short (VALIDATION_FAILED)."""
    with (
        sqlite_checkpointer(migrated_settings.database_path) as checkpointer,
        review_services(migrated_container) as reviews,
        TestClient(
            create_app(migrated_container, reviews), raise_server_exceptions=False
        ) as client,
    ):
        harness = build_harness(
            ScriptedReader(inconsistent_answer()),
            settings=agent_settings(max_attempts=1),
            uow_factory=migrated_container.uow_factory,
            checkpointer=checkpointer,
        )
        graph = harness.process_image().graph
        assert graph is not None
        yield Parked(client, harness, graph.receipt_id, require_id(graph.review_id))


def _issues(parked: Parked) -> list[dict[str, Any]]:
    issues = parked.harness.review(parked.review_id).details["issues"]
    assert isinstance(issues, list)
    return [
        {"code": issue["code"], "message": issue["message"], "position": issue["position"]}
        for issue in issues
        if isinstance(issue, dict)
    ]


def _corrected() -> dict[str, Any]:
    """The receipt as it really is: the sourdough is 9.98, so it adds up."""
    return {
        "store_name": "Costco",
        "purchased_on": "2026-11-01",
        "currency": "CAD",
        "subtotal_cents": 2555,
        "tax_cents": 128,
        "total_cents": 2683,
        "payment_last4": None,
        "lines": [
            _line("KS ORG EGGS 24CT", "1", 899),
            _line("KS 2% MILK 4L", "2", 658),
            _line("KS SOURDOUGH", "2", 998),
        ],
    }


FORCED = {"force": True, "reason": "checked the paper"}


def _short_import() -> ReceiptDraft:
    """A ``receipts import`` draft whose milk line is 6.85: it does not add up."""
    eggs, milk = receipt_draft().items
    return receipt_draft(items=(eggs, replace(milk, line_total=cad(685))))


def _line(raw_text: str, quantity: str, total: int, **overrides: object) -> dict[str, Any]:
    return {
        "kind": "item",
        "raw_text": raw_text,
        "description": raw_text.title(),
        "quantity": quantity,
        "unit_price_cents": None,
        "line_total_cents": total,
        "category": None,
        "applies_to": None,
        **overrides,
    }


def test_the_list_shows_each_open_review_with_its_reading(parked: Parked) -> None:
    response = parked.client.get("/api/reviews")

    assert response.status_code == 200, response.text
    created_at = parked.harness.review(parked.review_id).created_at
    assert response.json() == [
        {
            "id": parked.review_id,
            "receipt_id": parked.receipt_id,
            "reason": "validation_failed",
            "issues": _issues(parked),
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "reading": {
                "store_name": STORE_NAME,
                "purchased_on": "2026-11-01",
                "total": {"amount_cents": 2683, "currency": "CAD"},
                "item_count": 3,
            },
        }
    ]
    assert [(issue["code"], issue["position"]) for issue in _issues(parked)] == [
        ("line_math", 2),
        ("items_vs_subtotal", None),
    ]


def test_a_review_shows_the_full_reading_and_its_photo(parked: Parked) -> None:
    response = parked.client.get(parked.url)

    assert response.status_code == 200, response.text
    body = response.json()
    assert {key: body[key] for key in ("status", "receipt_status", "image_url")} == {
        "status": "open",
        "receipt_status": "needs_review",
        "image_url": f"/api/receipts/{parked.receipt_id}/image",
    }
    assert body["issues"] == _issues(parked)
    draft = body["draft"]
    assert {key: value for key, value in draft.items() if key != "lines"} == {
        "store_name": STORE_NAME,
        "purchased_on": "2026-11-01",
        "currency": "CAD",
        "subtotal_cents": 2555,
        "tax_cents": 128,
        "total_cents": 2683,
        "payment_last4": "1111",
    }
    assert [
        (line["kind"], line["raw_text"], line["quantity"], line["line_total_cents"])
        for line in draft["lines"]
    ] == [
        ("item", "KS ORG EGGS 24CT", "1", 899),
        ("item", "KS 2% MILK 4L", "2", 658),
        ("item", "KS SOURDOUGH", "2", 989),
    ]


def test_an_inconsistent_answer_is_a_problem_listing_each_issue(parked: Parked) -> None:
    """Accepted as read, the answer is judged on the issues the review was opened with (the
    sourdough's 2 x 4.99 against 9.89 among them), not on a draft rebuilt to hide them."""
    response = parked.client.post(f"{parked.url}/resolve", json={"accept": True})

    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    problem = response.json()
    assert problem["type"] == "/problems/inconsistent-receipt"
    line_math, short_sum = _issues(parked)
    assert problem["errors"] == [
        {"loc": ["body", "lines", 2], "msg": line_math["message"], "type": "line_math"},
        {"loc": ["body"], "msg": short_sum["message"], "type": "items_vs_subtotal"},
    ]
    assert parked.receipt_state() == ("needs_review", "needs_review")
    with parked.harness.uow_factory(read_only=True) as uow:
        assert uow.jobs.latest(JobKind.RESOLVE_REVIEW, parked.receipt_id) is None


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"accept": True, "force": True}, "forcing a draft that does not add up needs a reason"),
        ({"accept": True, "force": True, "reason": "  "}, "forcing a draft"),
        ({}, "The request is invalid."),
        ({"accept": True, "draft": _corrected()}, "The request is invalid."),
        ({"draft": {**_corrected(), "total_cents": 26.83}}, "The request is invalid."),
        (
            {"draft": {**_corrected(), "lines": [_line("X", "1", 100, applies_to=0)]}},
            "",
        ),
        *(
            ({"draft": {**_corrected(), "currency": currency}}, "The request is invalid.")
            for currency in ("cad", "CA", "CADX", "", "C4D")
        ),
        (
            {"draft": {key: value for key, value in _corrected().items() if key != "currency"}},
            "The request is invalid.",
        ),
        (
            {"draft": {**_corrected(), "purchased_on": "2099-01-01"}, **FORCED},
            "the purchase date 2099-01-01 is after today",
        ),
    ],
    ids=[
        "force-without-reason",
        "blank-reason",
        "no-answer",
        "both",
        "float-cents",
        "self-link",
        "currency-lowercase",
        "currency-short",
        "currency-long",
        "currency-empty",
        "currency-digit",
        "currency-missing",
        "future-date",
    ],
)
def test_an_invalid_answer_is_a_422_problem_and_queues_nothing(
    parked: Parked, body: dict[str, Any], detail: str
) -> None:
    response = parked.client.post(f"{parked.url}/resolve", json=body)

    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["detail"].startswith(detail)
    assert parked.receipt_state() == ("needs_review", "needs_review")
    with parked.harness.uow_factory(read_only=True) as uow:
        assert uow.jobs.latest(JobKind.RESOLVE_REVIEW, parked.receipt_id) is None


def test_a_corrected_reading_is_queued_once_and_saved_by_the_worker(parked: Parked) -> None:
    receipt_url = f"/api/receipts/{parked.receipt_id}"
    assert parked.client.get(receipt_url).json()["review_id"] == parked.review_id

    response = parked.client.post(f"{parked.url}/resolve", json=FORCED | {"draft": _corrected()})

    assert response.status_code == 202, response.text
    assert response.json() == {
        "receipt_id": parked.receipt_id,
        "status": "processing",
        "stage": "queued",
    }
    assert parked.receipt_state() == ("processing", "queued")
    assert parked.client.get("/api/reviews").json() == []
    again = parked.client.post(f"{parked.url}/resolve", json=FORCED | {"draft": _corrected()})
    assert again.status_code == 409, again.text
    assert again.headers["content-type"].startswith(PROBLEM_JSON)

    assert parked.run_worker() is True

    assert parked.receipt_state() == ("completed", "done")
    assert parked.client.get(receipt_url).json()["review_id"] is None
    detail = parked.client.get(parked.url).json()
    assert (detail["status"], detail["receipt_status"], detail["draft"]) == (
        "resolved",
        "completed",
        None,
    )
    assert parked.client.post(f"{parked.url}/resolve", json={"accept": True}).status_code == 409


def test_a_forced_accept_with_a_reason_is_queued(parked: Parked) -> None:
    response = parked.client.post(
        f"{parked.url}/resolve", json={"accept": True, "force": True, "reason": "torn receipt"}
    )

    assert response.status_code == 202, response.text
    assert parked.run_worker() is True
    assert parked.receipt_state() == ("completed", "done")


def test_an_imported_review_is_shown_and_answered_without_a_graph(parked: Parked) -> None:
    """I-02 / I-04: a review ``receipts import`` opened shows the stored receipt as its draft
    and its import issues; a corrected answer is saved in the request (202, completed)."""
    imported = parked.harness.record(_short_import())
    assert imported.review is not None
    url = f"/api/reviews/{imported.review.id}"
    receipt_id = require_id(imported.receipt.id)

    shown = parked.client.get(url).json()
    response = parked.client.post(f"{url}/resolve", json=FORCED | {"draft": _corrected()})

    assert (shown["receipt_status"], shown["answer_queued"]) == ("needs_review", False)
    assert [issue["code"] for issue in shown["issues"]] == ["line_math", "items_vs_subtotal"]
    assert [(line["raw_text"], line["line_total_cents"]) for line in shown["draft"]["lines"]] == [
        ("KS ORG EGGS 24CT", 899),
        ("KS 2% MILK 4L", 685),
    ]
    assert response.status_code == 202, response.text
    assert response.json() == {"receipt_id": receipt_id, "status": "completed", "stage": "done"}
    assert parked.client.get(f"/api/receipts/{receipt_id}").json()["status"] == "completed"
    assert parked.client.post(f"{url}/resolve", json={"accept": True}).status_code == 409


def test_a_stored_future_date_is_shown_but_never_answered(parked: Parked) -> None:
    """The date is checked on answers only: a review of a receipt stored with a future date
    opens, and answering with that date is a 422."""
    imported = parked.harness.record(replace(_short_import(), purchased_on=date(2099, 1, 1)))
    assert imported.review is not None
    url = f"/api/reviews/{imported.review.id}"

    shown = parked.client.get(url)
    answered = parked.client.post(f"{url}/resolve", json={"draft": shown.json()["draft"], **FORCED})

    assert shown.status_code == 200, shown.text
    assert shown.json()["draft"]["purchased_on"] == "2099-01-01"
    assert answered.status_code == 422, answered.text


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("GET", "/api/reviews/424242", None),
        ("POST", "/api/reviews/424242/resolve", {"accept": True}),
    ],
)
def test_an_unknown_review_is_a_404_problem(
    parked: Parked, method: str, url: str, body: object
) -> None:
    response = parked.client.request(method, url, json=body)

    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


def test_without_review_services_the_endpoints_are_503_problems(
    migrated_client: TestClient,
) -> None:
    response = migrated_client.get("/api/reviews")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


def test_a_receipts_photo_is_served_as_jpeg(
    migrated_settings: Settings, migrated_container: Container, migrated_client: TestClient
) -> None:
    stored = image_store(migrated_settings).save(png_bytes())
    with_photo = in_unit(
        migrated_container.uow_factory,
        lambda uow: uow.receipts.add(receipt(image_path=stored.relative_path)),
    )
    without_photo = in_unit(
        migrated_container.uow_factory, lambda uow: uow.receipts.add(receipt(image_path=None))
    )

    response = migrated_client.get(f"/api/receipts/{require_id(with_photo.id)}/image")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == image_store(migrated_settings).open(stored.relative_path)
    for receipt_id in (require_id(without_photo.id), 424242):
        missing = migrated_client.get(f"/api/receipts/{receipt_id}/image")
        assert missing.status_code == 404
        assert missing.headers["content-type"].startswith(PROBLEM_JSON)
