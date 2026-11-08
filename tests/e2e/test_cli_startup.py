"""CLI start-up: commands that run no agent never import the agents' heavy dependencies.

``db upgrade`` runs in every container start (the ``migrate`` service), ``worker
--check-heartbeat`` in every health check and ``--help`` whenever someone is lost; LangGraph,
the MCP SDK, the model adapters' HTTP client and the web stack cost about 1.5 s of imports
that these commands do not use. ``interfaces/cli/main.py`` therefore imports them inside the
commands that need them; this test runs the commands in a fresh interpreter (the test process
has imported everything already) and lists what got loaded.
"""

import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

from tests.support import run_cli

# Top-level packages only the agent commands, `serve` or `openapi` need.
HEAVY = frozenset(
    {"langgraph", "langchain_core", "langsmith", "mcp", "httpx", "fastapi", "uvicorn"}
)

LIGHT_COMMANDS = (
    ("--help",),
    ("db", "--help"),
    ("db", "upgrade"),
    ("db", "current"),
    ("worker", "--check-heartbeat"),
    ("version",),
)

# Runs each command through the real Typer app in one interpreter and prints, per command, the
# heavy packages loaded so far (a command's own exit is a SystemExit, which is expected).
PROBE = """
import json, sys
from paper_trail.interfaces.cli.main import app
heavy, commands = set(json.loads(sys.argv[1])), json.loads(sys.argv[2])
loaded = {}
for argv in commands:
    try:
        app(argv, prog_name="paper-trail")
    except SystemExit:
        pass
    loaded[" ".join(argv)] = sorted(heavy & {name.partition(".")[0] for name in sys.modules})
print(json.dumps(loaded))
"""


def test_commands_without_agents_do_not_import_the_heavy_dependencies(tmp_path: Path) -> None:
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("PAPER_TRAIL_")},
        "PAPER_TRAIL_DATA_DIR": str(tmp_path / "data"),
        "PAPER_TRAIL_STATIC_DIR": str(tmp_path / "no-static"),
        "PAPER_TRAIL_LOG_FORMAT": "json",
    }
    argv = [sys.executable, "-c", PROBE, json.dumps(sorted(HEAVY)), json.dumps(LIGHT_COMMANDS)]

    result = run_cli(argv, env)

    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout.splitlines()[-1])
    assert loaded == {" ".join(command): [] for command in LIGHT_COMMANDS}
    assert (tmp_path / "data" / "paper_trail.db").is_file(), "db upgrade really ran"


# Counts the lookups of this package's version through importlib.metadata while importing the
# CLI and running commands that do not print it, then reads the version.
VERSION_PROBE = """
import importlib.metadata, json, sys
lookups = []
real = importlib.metadata.version
def counted(name):
    if name == "paper-trail":
        lookups.append(name)
    return real(name)
importlib.metadata.version = counted
import paper_trail
from paper_trail.interfaces.cli.main import app
for argv in (["--help"], ["db", "--help"]):
    try:
        app(argv, prog_name="paper-trail")
    except SystemExit:
        pass
before = len(lookups)
version = paper_trail.__version__
print(json.dumps({"before": before, "after": len(lookups), "version": version}))
"""


def test_the_version_is_looked_up_only_when_asked_for(tmp_path: Path) -> None:
    """Test-suite cleanup review note: ``importlib.metadata.version()`` costs about 0.28 s, so
    ``paper_trail.__version__`` is resolved lazily (on first access), not on every import."""
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("PAPER_TRAIL_")},
        "PAPER_TRAIL_DATA_DIR": str(tmp_path / "data"),
        "PAPER_TRAIL_STATIC_DIR": str(tmp_path / "no-static"),
        "PAPER_TRAIL_LOG_FORMAT": "json",
    }

    result = run_cli([sys.executable, "-c", VERSION_PROBE], env)

    assert result.returncode == 0, result.stderr
    probe = json.loads(result.stdout.splitlines()[-1])
    assert probe["before"] == 0
    assert probe["after"] == 1
    assert probe["version"] == version("paper-trail")
