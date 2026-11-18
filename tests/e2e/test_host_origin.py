"""The web app's Host and Origin checks (hardening H1-01 to H1-09, ADR-0020).

Pinned: the built-in hosts (``localhost``, ``127.0.0.1``, ``web``) and the configured ones are
served with or without a port, the configured list adding to the built-ins; any other ``Host``
is a 400 problem before any handler runs (look-alikes included). A request that changes data
with an ``Origin`` other than its own (scheme, host and port; ``null`` included) is a 403 problem
and changes nothing; its own origin (default port or not) and no ``Origin`` are served. Safe
methods are served whatever their ``Origin``, and no CORS header is ever added. ``testserver``
(TestClient's host) is allowed by the test settings only.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from paper_trail.bootstrap import Container, build_container, dispose
from paper_trail.config import WebSettings
from paper_trail.interfaces.web.app import create_app
from tests.factories import product
from tests.images import png_bytes
from tests.storage import in_unit, stored_state
from tests.support import TEST_CLIENT_HOST, CopyMigrated, MakeSettings, require_id

CONFIGURED = ("192.168.0.187", "fabio-server", "fd00::5")
PROBLEM_JSON = "application/problem+json"


@pytest.fixture
def container(make_settings: MakeSettings, copy_migrated: CopyMigrated) -> Iterator[Container]:
    settings = make_settings(web=WebSettings(allowed_hosts=(TEST_CLIENT_HOST, *CONFIGURED)))
    copy_migrated(settings.database_path)
    built = build_container(settings)
    try:
        yield built
    finally:
        dispose(built)


@pytest.fixture
def client(container: Container) -> Iterator[TestClient]:
    with TestClient(create_app(container), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:8420",
        "127.0.0.1",
        "127.0.0.1:8000",
        "web",
        "web:8000",
        "192.168.0.187",
        "192.168.0.187:8420",
        "FABIO-SERVER:8420",
        "[::1]",
        "[::1]:8420",
        "[FD00::5]:8420",
    ],
)
def test_h1_01_h1_03_built_in_and_configured_hosts_are_served(
    client: TestClient, host: str
) -> None:
    assert client.get("/api/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host",
    ["evil.example", "192.168.0.50", "web.evil.example", "evil.example:8000", "", "[fd00::6]:80"],
)
@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/api/health"), ("POST", "/api/receipts"), ("GET", "/scan")],
    ids=["api-get", "upload", "spa"],
)
def test_h1_02_any_other_host_is_a_400_problem_before_any_handler(
    client: TestClient, container: Container, host: str, method: str, path: str
) -> None:
    files = {"file": ("r.png", png_bytes(), "image/png")} if method == "POST" else None

    response = client.request(method, path, headers={"Host": host}, files=files)

    assert response.status_code == 400
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    state = stored_state(container.engine)
    assert (state.receipts, state.jobs) == ({}, {})


def test_h1_09_default_settings_refuse_the_test_clients_host(
    make_settings: MakeSettings, copy_migrated: CopyMigrated
) -> None:
    settings = make_settings(web=WebSettings())
    copy_migrated(settings.database_path)
    container = build_container(settings)
    try:
        with TestClient(create_app(container)) as default_client:
            refused = default_client.get("/api/health")
            served = default_client.get("/api/health", headers={"Host": "web:8000"})
    finally:
        dispose(container)

    assert (refused.status_code, served.status_code) == (400, 200)


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "http://testserver:8080",
        "https://testserver",
        "null",
        "http://localhost:8000",
    ],
)
def test_h1_05_an_upload_from_another_origin_is_a_403_problem_and_stores_nothing(
    client: TestClient, container: Container, origin: str
) -> None:
    response = client.post(
        "/api/receipts",
        headers={"Origin": origin},
        files={"file": ("r.png", png_bytes(), "image/png")},
    )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json() == {
        "type": "about:blank",
        "title": "Forbidden",
        "status": 403,
        "detail": "Requests from another site are not accepted.",
        "instance": "/api/receipts",
        "errors": None,
    }
    state = stored_state(container.engine)
    assert (state.receipts, state.jobs) == ({}, {})


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/api/products/{product_id}"),
        ("POST", "/api/reviews/1/resolve"),
        ("PUT", "/api/receipts/1"),
        ("DELETE", "/api/receipts/1"),
    ],
    ids=["product-edit", "review-answer", "put", "delete"],
)
def test_h1_05_every_method_that_changes_data_is_checked(
    client: TestClient, container: Container, method: str, path: str
) -> None:
    stored = in_unit(container.uow_factory, lambda uow: uow.products.add(product()))
    before = stored_state(container.engine)

    response = client.request(
        method,
        path.format(product_id=require_id(stored.id)),
        headers={"Origin": "http://evil.example"},
        json={"canonical_name": "Hacked"} if method == "PATCH" else None,
    )

    assert response.status_code == 403
    assert stored_state(container.engine) == before


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        (TEST_CLIENT_HOST, "http://testserver"),
        ("web:8000", "http://web:8000"),
        ("web", "http://web"),
        ("web:80", "http://web"),
        ("web", "http://web:80"),
        ("192.168.0.187:8420", "http://192.168.0.187:8420"),
        ("[::1]:8420", "http://[::1]:8420"),
    ],
)
def test_h1_06_a_write_from_its_own_origin_is_served(
    client: TestClient, host: str, origin: str
) -> None:
    response = client.patch(
        "/api/products/424242",
        headers={"Host": host, "Origin": origin},
        json={"canonical_name": "Eggs"},
    )

    assert response.status_code == 404


def test_h1_07_a_write_without_an_origin_is_served(client: TestClient) -> None:
    response = client.patch("/api/products/424242", json={"canonical_name": "Eggs"})

    assert response.status_code == 404


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_h1_08_safe_methods_are_served_whatever_their_origin_without_cors(
    client: TestClient, method: str
) -> None:
    response = client.request(method, "/api/health", headers={"Origin": "http://evil.example"})

    assert response.status_code != 403
    assert not any(name.startswith("access-control-") for name in response.headers)
