"""OpenAPI contract (S0-AC-21, S0-AC-32), injected configuration (S0-AC-14), no CORS (S0-AC-40)."""

import json
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from paper_trail.bootstrap import build_container, dispose
from paper_trail.interfaces.web.app import create_app
from tests.support import REPO_ROOT, MakeSettings

HTTP_METHODS = {"get", "put", "post", "delete", "patch", "options", "head", "trace"}
PROBLEM_JSON = "application/problem+json"
COMMITTED_SPEC = REPO_ROOT / "frontend" / "openapi.json"


def _operations(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


def test_s0_ac_21_every_operation_has_an_explicit_operation_id(app: FastAPI) -> None:
    """S0-AC-21: every route has an explicit camelCase operationId (stable TS client names)."""
    operations = _operations(app.openapi())

    assert operations
    ids = {op["operationId"] for _, _, op in operations}
    assert len(ids) == len(operations), "operationIds must be unique"
    for operation_id in ids:
        assert operation_id[0].islower()
        assert "_" not in operation_id, f"not camelCase (FastAPI default?): {operation_id}"


def test_s0_ac_21_stage0_operations_are_named(app: FastAPI) -> None:
    """S0-AC-21 / PLAN 2.9: the Stage 0 routes are getHealth and getReadiness."""
    spec = app.openapi()

    assert spec["paths"]["/api/health"]["get"]["operationId"] == "getHealth"
    assert spec["paths"]["/api/health/ready"]["get"]["operationId"] == "getReadiness"


def test_s0_ac_21_spa_and_docs_are_not_in_the_contract(app: FastAPI) -> None:
    """S0-AC-21: only /api routes are part of the contract the TS client is generated from."""
    for path, _, _ in _operations(app.openapi()):
        assert path.startswith("/api/"), path


def test_s0_ac_21_problem_responses_are_declared(app: FastAPI) -> None:
    """S0-AC-21: every operation declares a 500 problem+json response; ProblemDetail is a schema."""
    spec = app.openapi()

    assert "ProblemDetail" in spec["components"]["schemas"]
    for path, method, operation in _operations(spec):
        response = operation["responses"].get("500")
        assert response is not None, f"{method.upper()} {path} does not declare 500"
        assert PROBLEM_JSON in response["content"], f"{method.upper()} {path} 500 not problem+json"


def test_s0_ac_21_readiness_declares_its_503(app: FastAPI) -> None:
    """S0-AC-21 / PLAN 2.9: getReadiness documents 503 with the ReadinessOut body."""
    operation = app.openapi()["paths"]["/api/health/ready"]["get"]
    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
    unavailable = operation["responses"]["503"]["content"]["application/json"]["schema"]

    assert ok == unavailable


def test_s0_ac_32_committed_spec_matches_the_backend(app: FastAPI) -> None:
    """S0-AC-32: frontend/openapi.json equals the generated spec (drift -> `make api-client`)."""
    committed = json.loads(COMMITTED_SPEC.read_text(encoding="utf-8"))

    assert committed == app.openapi()


def test_s0_ac_32_drift_check_detects_a_changed_route(app: FastAPI) -> None:
    """S0-AC-32: a modified backend route schema no longer matches the committed spec."""
    committed = json.loads(COMMITTED_SPEC.read_text(encoding="utf-8"))

    @app.get("/api/health/extra", operation_id="getExtra")
    def extra() -> dict[str, str]:
        return {}

    app.openapi_schema = None
    assert committed != app.openapi()


def test_s0_ac_14_two_apps_with_different_settings_coexist(make_settings: MakeSettings) -> None:
    """S0-AC-14: configuration is injected; two apps in one process do not share settings."""
    first = build_container(make_settings(build_sha="1111111"))
    second = build_container(make_settings(build_sha="2222222"))
    try:
        with TestClient(create_app(first)) as a, TestClient(create_app(second)) as b:
            assert a.get("/api/health").json()["build_sha"] == "1111111"
            assert b.get("/api/health").json()["build_sha"] == "2222222"
    finally:
        dispose(first)
        dispose(second)


def test_s0_ac_40_no_cors_middleware_is_installed(app: FastAPI) -> None:
    """S0-AC-40: same-origin only; no CORSMiddleware (ADR-0016)."""
    assert all(middleware.cls is not CORSMiddleware for middleware in app.user_middleware)  # type: ignore[comparison-overlap]


def test_s0_ac_40_cross_origin_requests_get_no_cors_headers(client: TestClient) -> None:
    """S0-AC-40: neither simple nor preflight requests get Access-Control-Allow-* headers."""
    origin = {"Origin": "http://evil.example"}
    simple = client.get("/api/health", headers=origin)
    preflight = client.options(
        "/api/health", headers={**origin, "Access-Control-Request-Method": "GET"}
    )

    for response in (simple, preflight):
        assert "access-control-allow-origin" not in response.headers
        assert "access-control-allow-credentials" not in response.headers
