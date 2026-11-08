"""Shared constants and fixture type aliases for the backend test suites.

Kept out of conftest.py so test modules never import a conftest directly.
"""

import socket
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from paper_trail.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]

INDEX_HTML = (
    "<!doctype html><html><head><title>Paper Trail</title></head>"
    '<body><div id="root"></div></body></html>'
)
HASHED_ASSET = "assets/index-3f9a1c2b.js"

# The newest Alembic revision; bump it with every migration (agent core A adds 0003_agent_core).
SCHEMA_HEAD = "0003"

MakeSettings = Callable[..., Settings]
CopyMigrated = Callable[[Path], Path]
JsonLogs = Callable[[], list[dict[str, Any]]]
InsertRoute = Callable[[FastAPI, str, Callable[..., Any]], None]


def require_id(value: int | None) -> int:
    """The id of a stored entity; fails the test when an entity that must be stored has none."""
    if value is None:
        msg = "expected a stored entity with an id"
        raise AssertionError(msg)
    return value


def cli_command(*args: str) -> list[str]:
    """argv that runs the ``paper-trail`` CLI the way the containers do."""
    return [sys.executable, "-m", "paper_trail", *args]


def run_cli(args: Sequence[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv built from sys.executable
        list(args), env=env, capture_output=True, text=True, timeout=60, check=False
    )


def free_port() -> int:
    """A loopback TCP port nobody listens on now, for a server process the test starts.

    The OS picks it (bind to port 0), so parallel test workers do not collide on a fixed port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port
