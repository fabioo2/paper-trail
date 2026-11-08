"""Liveness and readiness endpoints through the fully wired app (S0-AC-17, S0-AC-18)."""

from importlib.metadata import version
from pathlib import Path

from fastapi.testclient import TestClient

from paper_trail.bootstrap import build_container, dispose
from paper_trail.interfaces.web.app import create_app
from tests.support import SCHEMA_HEAD, MakeSettings

DATABASE_DATA_KEYS = {"revision", "head", "sqlite_version", "sqlite_vec"}


def test_s0_ac_17_health_returns_status_version_and_build_sha(client: TestClient) -> None:
    """S0-AC-17: GET /api/health -> 200 {"status":"ok","version":<pkg>,"build_sha":null}."""
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "status": "ok",
        "version": version("paper-trail"),
        "build_sha": None,
    }


def test_s0_ac_17_health_reports_configured_build_sha(make_settings: MakeSettings) -> None:
    """S0-AC-17: build_sha comes from settings (PAPER_TRAIL_BUILD_SHA in the image)."""
    container = build_container(make_settings(build_sha="abc1234"))
    try:
        with TestClient(create_app(container)) as client:
            assert client.get("/api/health").json()["build_sha"] == "abc1234"
    finally:
        dispose(container)


def test_s0_ac_17_health_does_not_touch_the_database(client: TestClient, tmp_path: Path) -> None:
    """S0-AC-17: liveness performs no I/O; it passes while the DB file is absent."""
    database = tmp_path / "data" / "paper_trail.db"

    response = client.get("/api/health")

    assert response.status_code == 200
    assert not database.exists(), "liveness must not create or open the database"


def test_s0_ac_18_ready_when_migrated(migrated_client: TestClient) -> None:
    """S0-AC-18: 200 with a database component carrying revision/head/sqlite/vec data."""
    response = migrated_client.get("/api/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    (component,) = [c for c in body["components"] if c["name"] == "database"]
    assert component["status"] == "ok"
    assert component["detail"] is None
    assert component["data"].keys() >= DATABASE_DATA_KEYS
    assert component["data"]["revision"] == component["data"]["head"] == SCHEMA_HEAD
    assert component["data"]["sqlite_vec"].startswith("v")


def test_s0_ac_18_not_ready_when_database_not_migrated(client: TestClient) -> None:
    """S0-AC-18: 503 with status "error" and a detail when the schema is not at head."""
    response = client.get("/api/health/ready")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["status"] == "error"
    (component,) = [c for c in body["components"] if c["name"] == "database"]
    assert component["status"] == "error"
    assert component["detail"]


def test_s0_ac_18_not_ready_when_database_unreadable(
    make_settings: MakeSettings, tmp_path: Path
) -> None:
    """S0-AC-18: a file that is not a SQLite database yields 503, not a 500."""
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a sqlite database" * 64)
    container = build_container(make_settings(database_path=corrupt))
    try:
        with TestClient(create_app(container), raise_server_exceptions=False) as client:
            response = client.get("/api/health/ready")
    finally:
        dispose(container)

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["components"][0]["detail"]


def test_s0_ac_18_readiness_body_shape_is_identical_for_200_and_503(
    migrated_client: TestClient, make_settings: MakeSettings, tmp_path: Path
) -> None:
    """S0-AC-18: 503 uses the same ReadinessOut shape (not problem+json) so monitors can read it."""
    ok = migrated_client.get("/api/health/ready").json()
    container = build_container(make_settings(data_dir=tmp_path / "unmigrated"))
    try:
        with TestClient(create_app(container)) as client:
            not_ready = client.get("/api/health/ready").json()
    finally:
        dispose(container)

    assert ok.keys() == not_ready.keys() == {"status", "components"}
    assert ok["components"][0].keys() == not_ready["components"][0].keys()
