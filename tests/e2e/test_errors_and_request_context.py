"""Problem-details errors (S0-AC-19) and request correlation (S0-AC-16)."""

import re
from collections.abc import Iterator

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from paper_trail.bootstrap import Container
from paper_trail.domain.errors import (
    ConflictError,
    DomainError,
    DomainValidationError,
    NotFoundError,
)
from paper_trail.interfaces.web.app import create_app
from paper_trail.observability import configure_logging
from tests.support import InsertRoute, JsonLogs

PROBLEM_JSON = "application/problem+json"
GENERATED_ID = re.compile(r"^[0-9a-f]{32}$")
LEAKY_MESSAGE = "internal detail 7f3a must not leak"


@pytest.fixture
def logged_app(container: Container, insert_route: InsertRoute) -> FastAPI:
    """App with JSON logging at DEBUG and a few routes that raise on purpose."""
    configure_logging(container.settings.model_copy(update={"log_level": "DEBUG"}))
    app = create_app(container)

    def explode() -> None:
        raise RuntimeError(LEAKY_MESSAGE)

    def missing() -> None:
        raise NotFoundError("receipt 7 does not exist")

    def conflict() -> None:
        raise ConflictError("already processed")

    def invalid() -> None:
        raise DomainValidationError("total does not add up")

    def logs_inside() -> dict[str, str]:
        structlog.get_logger("paper_trail.test").info("inside.handler")
        return {"ok": "yes"}

    def typed(limit: int) -> dict[str, int]:
        return {"limit": limit}

    for path, endpoint in [
        ("/api/_test/explode", explode),
        ("/api/_test/missing", missing),
        ("/api/_test/conflict", conflict),
        ("/api/_test/invalid", invalid),
        ("/api/_test/logs", logs_inside),
        ("/api/_test/typed", typed),
    ]:
        insert_route(app, path, endpoint)  # type: ignore[arg-type]
    return app


@pytest.fixture
def logged_client(logged_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(logged_app, raise_server_exceptions=False) as client:
        yield client


def _assert_problem(response_json: dict[str, object], status: int, type_: str) -> None:
    assert response_json["status"] == status
    assert response_json["type"] == type_
    assert isinstance(response_json["title"], str)
    assert response_json["title"]


# --- S0-AC-19 -------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/nope", "/api/health/nope", "/api/receipts/1/x"])
def test_s0_ac_19_unknown_api_path_is_problem_404(client: TestClient, path: str) -> None:
    """S0-AC-19: unknown /api/* paths return 404 application/problem+json."""
    response = client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    _assert_problem(response.json(), 404, "/problems/not-found")


def test_s0_ac_19_unhandled_exception_is_generic_500(logged_client: TestClient) -> None:
    """S0-AC-19: a 500 problem body leaks neither the traceback nor the exception message."""
    response = logged_client.get("/api/_test/explode")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    _assert_problem(response.json(), 500, "/problems/internal")
    assert LEAKY_MESSAGE not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


def test_s0_ac_19_unhandled_exception_logs_traceback_with_request_id(
    logged_client: TestClient, json_logs: JsonLogs
) -> None:
    """S0-AC-19: the full traceback is logged at ERROR with the request_id."""
    json_logs()  # discard startup lines
    response = logged_client.get("/api/_test/explode", headers={"X-Request-ID": "trace-me-1"})

    errors = [r for r in json_logs() if r["level"] == "error"]
    assert errors, "expected an ERROR log line for the unhandled exception"
    (error,) = [r for r in errors if r.get("request_id") == "trace-me-1"]
    assert LEAKY_MESSAGE in str(error["exception"])
    assert "Traceback" in str(error["exception"])
    assert response.headers["X-Request-ID"] == "trace-me-1"


@pytest.mark.parametrize(
    ("path", "status", "type_"),
    [
        ("/api/_test/missing", 404, "/problems/not-found"),
        ("/api/_test/conflict", 409, "/problems/conflict"),
        ("/api/_test/invalid", 422, "/problems/validation"),
    ],
)
def test_s0_ac_19_domain_errors_map_to_problem_details(
    logged_client: TestClient, path: str, status: int, type_: str
) -> None:
    """S0-AC-19 (PLAN 1.5): domain errors map to their documented status and problem type."""
    response = logged_client.get(path)

    assert response.status_code == status
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    _assert_problem(body, status, type_)
    assert body["detail"]


def test_s0_ac_19_domain_errors_share_a_base_class() -> None:
    """S0-AC-19 (PLAN 1.5): every domain error derives from DomainError."""
    for error in (NotFoundError, ConflictError, DomainValidationError):
        assert issubclass(error, DomainError)


def test_s0_ac_19_request_validation_lists_field_errors(logged_client: TestClient) -> None:
    """S0-AC-19 (PLAN 1.5): RequestValidationError -> 422 problem with errors[loc,msg,type]."""
    response = logged_client.get("/api/_test/typed", params={"limit": "many"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    _assert_problem(body, 422, "/problems/validation")
    (error,) = body["errors"]
    assert error.keys() >= {"loc", "msg", "type"}
    assert error["loc"][-1] == "limit"


# --- S0-AC-16 -------------------------------------------------------------------------------


def test_s0_ac_16_valid_incoming_request_id_is_echoed(client: TestClient) -> None:
    """S0-AC-16: a valid X-Request-ID (^[A-Za-z0-9-]{1,64}$) is echoed unchanged."""
    request_id = "Phone-Upload-" + "a" * 51  # 64 chars, the maximum

    response = client.get("/api/health", headers={"X-Request-ID": request_id})

    assert response.headers["X-Request-ID"] == request_id


@pytest.mark.parametrize(
    "incoming",
    [None, "", "has space", "semi;colon", "under_score", "x" * 65, "é-unicode"],
)
def test_s0_ac_16_missing_or_invalid_request_id_is_replaced(
    client: TestClient, incoming: str | None
) -> None:
    """S0-AC-16: missing or invalid ids are replaced by a generated 32-char hex id."""
    # Sent as UTF-8 bytes: the HTTP client refuses non-ASCII str header values itself.
    headers = {} if incoming is None else {b"X-Request-ID": incoming.encode()}

    response = client.get("/api/health", headers=headers)

    assert GENERATED_ID.fullmatch(response.headers["X-Request-ID"])


def test_s0_ac_16_generated_ids_are_unique(client: TestClient) -> None:
    """S0-AC-16: each request without an id gets its own id."""
    ids = {client.get("/api/health").headers["X-Request-ID"] for _ in range(5)}

    assert len(ids) == 5


@pytest.mark.parametrize("path", ["/api/nope", "/scan", "/api/health/ready"])
def test_s0_ac_16_every_response_carries_a_request_id(client: TestClient, path: str) -> None:
    """S0-AC-16: 404s, SPA responses and 503s carry X-Request-ID too."""
    assert GENERATED_ID.fullmatch(client.get(path).headers["X-Request-ID"])


def test_s0_ac_16_one_access_line_per_request(
    logged_client: TestClient, json_logs: JsonLogs
) -> None:
    """S0-AC-16: one http.request line with method, path, status, duration_ms."""
    json_logs()
    logged_client.get("/api/_test/logs", headers={"X-Request-ID": "access-1"})

    access = [r for r in json_logs() if r["event"] == "http.request"]
    assert len(access) == 1
    (line,) = access
    assert line["request_id"] == "access-1"
    assert line["method"] == "GET"
    assert line["path"] == "/api/_test/logs"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], int | float)
    assert line["duration_ms"] >= 0


def test_s0_ac_16_all_lines_during_a_request_share_its_request_id(
    logged_client: TestClient, json_logs: JsonLogs
) -> None:
    """S0-AC-16: every log line emitted while handling a request carries the same request_id."""
    json_logs()
    logged_client.get("/api/_test/logs", headers={"X-Request-ID": "corr-42"})

    records = json_logs()
    inside = [r for r in records if r["event"] in {"inside.handler", "http.request"}]
    assert len(inside) == 2
    assert {r["request_id"] for r in inside} == {"corr-42"}


def test_s0_ac_16_request_id_does_not_leak_into_the_next_request(
    logged_client: TestClient, json_logs: JsonLogs
) -> None:
    """S0-AC-16: context is cleared between requests."""
    logged_client.get("/api/_test/logs", headers={"X-Request-ID": "first"})
    json_logs()
    logged_client.get("/api/_test/logs", headers={"X-Request-ID": "second"})

    ids = {r.get("request_id") for r in json_logs() if r["event"] == "inside.handler"}
    assert ids == {"second"}


def test_s0_ac_16_health_access_lines_are_debug(
    logged_client: TestClient, json_logs: JsonLogs
) -> None:
    """S0-AC-16 / PLAN 1.4: health endpoints log their access line at DEBUG to avoid noise."""
    json_logs()
    logged_client.get("/api/health")

    (line,) = [r for r in json_logs() if r["event"] == "http.request"]
    assert line["level"] == "debug"
    assert line["path"] == "/api/health"
