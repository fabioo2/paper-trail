"""Serving the built SPA from FastAPI (S0-AC-20)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from paper_trail.bootstrap import build_container, dispose
from paper_trail.interfaces.web.app import create_app
from tests.support import HASHED_ASSET, MakeSettings

IMMUTABLE = "public, max-age=31536000, immutable"


@pytest.mark.parametrize("path", ["/", "/scan", "/receipts", "/review/anything", "/a/b/c"])
def test_s0_ac_20_client_routes_return_index_html(client: TestClient, path: str) -> None:
    """S0-AC-20: client-side routes fall back to index.html, text/html, no-cache."""
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert '<div id="root"></div>' in response.text


def test_s0_ac_20_hashed_assets_are_immutable(client: TestClient, static_dir: Path) -> None:
    """S0-AC-20: files under /assets/ get a one-year immutable cache policy."""
    response = client.get(f"/{HASHED_ASSET}")

    assert response.status_code == 200
    assert response.text == (static_dir / HASHED_ASSET).read_text(encoding="utf-8")
    assert response.headers["cache-control"] == IMMUTABLE
    assert "javascript" in response.headers["content-type"]


def test_s0_ac_20_missing_asset_is_404_not_index(client: TestClient) -> None:
    """S0-AC-20: a missing hashed asset must not be answered with index.html."""
    response = client.get("/assets/index-deadbeef.js")

    assert response.status_code == 404
    assert '<div id="root">' not in response.text


def test_s0_ac_20_manifest_has_its_media_type(client: TestClient) -> None:
    """S0-AC-20: the web app manifest is served as application/manifest+json, no-cache."""
    response = client.get("/manifest.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    assert response.headers["cache-control"] == "no-cache"
    assert response.json()["start_url"] == "/scan"


def test_s0_ac_20_other_existing_files_are_served_as_is(client: TestClient) -> None:
    """S0-AC-20: a file that exists in the static dir is returned instead of index.html."""
    response = client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in response.text


def test_s0_ac_20_path_traversal_is_not_served(client: TestClient, tmp_path: Path) -> None:
    """S0-AC-20: the SPA mount never serves files outside the static dir."""
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")

    response = client.get("/..%2Fsecret.txt")

    assert "top secret" not in response.text


def test_s0_ac_20_api_routes_take_precedence_over_spa(client: TestClient) -> None:
    """S0-AC-20: /api/* is never answered by the SPA fallback."""
    response = client.get("/api/health")

    assert response.headers["content-type"].startswith("application/json")


@pytest.fixture
def api_only_client(make_settings: MakeSettings, tmp_path: Path) -> Iterator[TestClient]:
    container = build_container(make_settings(static_dir=tmp_path / "does-not-exist"))
    try:
        with TestClient(create_app(container), raise_server_exceptions=False) as client:
            yield client
    finally:
        dispose(container)


def test_s0_ac_20_missing_static_dir_keeps_api_working(api_only_client: TestClient) -> None:
    """S0-AC-20: without a static dir the API still works (API-only mode)."""
    assert api_only_client.get("/api/health").status_code == 200


@pytest.mark.parametrize("path", ["/", "/scan", "/manifest.webmanifest"])
def test_s0_ac_20_missing_static_dir_returns_problem_404(
    api_only_client: TestClient, path: str
) -> None:
    """S0-AC-20: without a static dir, SPA routes return 404 problem+json."""
    response = api_only_client.get(path)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "/problems/not-found"
