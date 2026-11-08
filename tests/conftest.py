"""Shared pytest fixtures for every backend suite.

Suites (see docs/PLAN.md section 1.7 and docs/tests/stage0.md):

- ``tests/unit``        no I/O, fakes only (unmarked)
- ``tests/contract``    port contract suites run against fake and real adapters (unmarked)
- ``tests/integration`` real adapters on a temporary SQLite file (``integration`` marker)
- ``tests/e2e``         fully wired app / CLI processes on a temporary data dir (``e2e`` marker)
- ``tests/repo``        static checks of Makefile, compose, CI and docs (``repo`` marker)

Markers are applied from the directory, so individual tests never forget them.
"""

import ipaddress
import json
import logging
import os
import shutil
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import upgrade_to_head
from paper_trail.bootstrap import Container, build_container, dispose
from paper_trail.config import DATABASE_FILENAME, Settings
from paper_trail.interfaces.web.app import create_app
from tests.support import (
    HASHED_ASSET,
    INDEX_HTML,
    REPO_ROOT,
    CopyMigrated,
    InsertRoute,
    JsonLogs,
    MakeSettings,
)

_SUITE_MARKERS = {
    "integration": "real adapters against a temporary SQLite file",
    "e2e": "fully wired application or CLI process",
    "repo": "static checks of repository files",
}

_STDLIB_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "alembic", "httpx")

HASHED_ASSET_CONTENT = "console.log('paper trail');\n"


def pytest_configure(config: pytest.Config) -> None:
    for name, description in _SUITE_MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {description}")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        suite = Path(str(item.path)).relative_to(REPO_ROOT / "tests").parts[0]
        if suite in _SUITE_MARKERS:
            item.add_marker(getattr(pytest.mark, suite))


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test sees the developer's PAPER_TRAIL_* variables or a repo-root ``.env`` file."""
    for key in [k for k in os.environ if k.startswith("PAPER_TRAIL_")]:
        monkeypatch.delenv(key)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)


def _is_loopback(host: object) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _loopback_only_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test opens a network connection beyond this machine.

    The LLM host is the owner's gaming PC: tests use a scripted fake LLM or a fake Ollama server
    on 127.0.0.1, never a real model. A connection to any other address fails loudly here, so a
    stray ``.env`` or a forgotten override cannot reach a real host from an in-process test.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def check(sock: socket.socket, address: Any) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address[0]):
            msg = f"tests may only connect to loopback, not {address!r}"
            raise AssertionError(msg)

    def connect(sock: socket.socket, address: Any) -> None:
        check(sock, address)
        real_connect(sock, address)

    def connect_ex(sock: socket.socket, address: Any) -> int:
        check(sock, address)
        return real_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """configure_logging() mutates global logging state; put it back after every test."""
    root = logging.getLogger()
    saved_root = (root.level, list(root.handlers))
    saved = {
        name: (lg.level, list(lg.handlers), lg.propagate)
        for name in _STDLIB_LOGGERS
        for lg in [logging.getLogger(name)]
    }
    yield
    root.setLevel(saved_root[0])
    root.handlers[:] = saved_root[1]
    for name, (level, handlers, propagate) in saved.items():
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.handlers[:] = handlers
        lg.propagate = propagate
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    """A minimal built-SPA directory, shaped like ``frontend/dist``."""
    root = tmp_path / "static"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / HASHED_ASSET).write_text(HASHED_ASSET_CONTENT, encoding="utf-8")
    (root / "manifest.webmanifest").write_text(
        json.dumps({"name": "Paper Trail", "start_url": "/scan"}), encoding="utf-8"
    )
    (root / "favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>', encoding="utf-8"
    )
    return root


@pytest.fixture
def make_settings(tmp_path: Path, static_dir: Path) -> MakeSettings:
    """Build Settings rooted in a temporary data dir; keyword overrides win."""

    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "env": "test",
            "data_dir": tmp_path / "data",
            "static_dir": static_dir,
            "log_format": "json",
        }
        values.update(overrides)
        return Settings(**values)

    return factory


@pytest.fixture
def settings(make_settings: MakeSettings) -> Settings:
    return make_settings()


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database file migrated to the Alembic head once per session (per xdist worker).

    Tests never open it: they copy it (``copy_migrated``), which is much cheaper than running
    the migrations again. Disposing the engine checkpoints the WAL into the file.
    """
    path = tmp_path_factory.mktemp("migrated-template") / DATABASE_FILENAME
    engine = create_engine(path)
    try:
        upgrade_to_head(engine)
    finally:
        engine.dispose()
    assert not path.with_name(f"{path.name}-wal").exists(), "the template must be one file"
    return path


@pytest.fixture
def copy_migrated(migrated_template: Path) -> CopyMigrated:
    """``copy_migrated(path)`` puts a database at the Alembic head at ``path`` and returns it."""

    def copy(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(migrated_template, path)
        return path

    return copy


@pytest.fixture
def migrated_engine(tmp_path: Path, copy_migrated: CopyMigrated) -> Iterator[Engine]:
    """An engine on a temporary database file migrated to the Alembic head."""
    engine = create_engine(copy_migrated(tmp_path / "migrated" / DATABASE_FILENAME))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def migrated_settings(settings: Settings, copy_migrated: CopyMigrated) -> Settings:
    """Settings whose database exists and is at the Alembic head revision."""
    copy_migrated(settings.database_path)
    return settings


def _container(settings: Settings) -> Iterator[Container]:
    container = build_container(settings)
    try:
        yield container
    finally:
        dispose(container)


@pytest.fixture
def container(settings: Settings) -> Iterator[Container]:
    yield from _container(settings)


@pytest.fixture
def migrated_container(migrated_settings: Settings) -> Iterator[Container]:
    yield from _container(migrated_settings)


@pytest.fixture
def app(container: Container) -> FastAPI:
    return create_app(container)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def migrated_client(migrated_container: Container) -> Iterator[TestClient]:
    with TestClient(create_app(migrated_container), raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def insert_route() -> InsertRoute:
    """Register a throwaway GET route ahead of the SPA catch-all mount."""

    def insert(app: FastAPI, path: str, endpoint: Callable[..., Any]) -> None:
        app.router.routes.insert(0, APIRoute(path, endpoint, methods=["GET"]))

    return insert


@pytest.fixture
def json_logs(capsys: pytest.CaptureFixture[str]) -> JsonLogs:
    """Read everything written to stdout so far; every line must be one JSON object."""

    def read() -> list[dict[str, Any]]:
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        parsed: list[dict[str, Any]] = []
        for line in lines:
            record = json.loads(line)
            assert isinstance(record, dict), f"log line is not a JSON object: {line!r}"
            parsed.append(record)
        return parsed

    return read
