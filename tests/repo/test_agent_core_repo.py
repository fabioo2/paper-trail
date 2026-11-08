"""Repository wiring of the agent core: Make targets, the cli service, image and dependencies.

Agent-core plan 2.1 and 4.1, PLAN 1.3 (``PAPER_TRAIL_RECEIPTS_DIR``).

Pinned (docs/tests/agent-core-A.md): the user-facing run name variable is ``RUN_NAME``, not the
plan's ``RUN``: the Makefile's ``RUN`` is already ``$(DC) run --rm`` and a command-line
``RUN=...`` would override it in every recipe. ``make process RECEIPTS=<path> RUN_NAME=<run>
ARGS="<flags>"``; ``RECEIPTS`` defaults to ``.`` (the whole folder).
"""

import re
import tomllib
from typing import Any

import pytest

from tests.repo.conftest import read_text
from tests.repo.makefile_parser import Makefile

# A local ./receipts by default; the owner sets the real folder in the git-ignored .env.
RECEIPTS_MOUNT = "${PAPER_TRAIL_RECEIPTS_DIR:-./receipts}"
RUNTIME_DEPENDENCIES = (
    "langgraph",
    "langgraph-checkpoint-sqlite",
    "httpx",
    "pillow",
    "pillow-heif",
)
TESSERACT_PACKAGES = ("tesseract-ocr", "tesseract-ocr-eng")


def _recipe(makefile: Makefile, target: str) -> str:
    return "\n".join(makefile.rule(target).commands)


@pytest.mark.parametrize("target", ["process", "pt", "graph"])
def test_agent_core_make_targets_exist_with_help(makefile: Makefile, target: str) -> None:
    assert makefile.rule(target).help


def test_make_process_runs_the_cli_service(makefile: Makefile) -> None:
    """Plan 4.1: `make process` = `docker compose run --rm cli process /receipts/...`."""
    recipe = _recipe(makefile, "process")

    assert recipe.startswith("$(RUN) cli process ")
    assert '"/receipts/$(RECEIPTS)"' in recipe  # file names may contain spaces
    assert "$(if $(RUN_NAME),--run $(RUN_NAME))" in recipe
    assert recipe.rstrip().endswith("$(ARGS)")
    assert makefile.variables["RECEIPTS"] == "."


def test_make_pt_and_graph_run_the_cli_service(makefile: Makefile) -> None:
    assert _recipe(makefile, "pt") == "$(RUN) cli $(ARGS)"
    assert _recipe(makefile, "graph") == "$(RUN) cli graph --mermaid"


def test_the_compose_run_macro_is_not_a_user_variable(makefile: Makefile) -> None:
    """`make process RUN=x` would break every recipe; the run name is RUN_NAME."""
    assert makefile.variables["RUN"] == "$(DC) run --rm"
    assert "RUN=" not in read_text("README.md").replace("RUN_NAME=", "")


def test_the_cli_service(compose: dict[str, Any]) -> None:
    """Plan 4.1: profile `cli`, the app image, one-off, the receipts folder read-only."""
    cli = compose["services"]["cli"]

    assert cli["profiles"] == ["cli"]
    assert cli["restart"] == "no"
    assert "ports" not in cli
    assert f"{RECEIPTS_MOUNT}:/receipts:ro" in cli["volumes"]
    assert "${PAPER_TRAIL_DATA_VOLUME:-paper-trail-data}:/data" in cli["volumes"]
    assert cli["read_only"] is True
    assert cli["cap_drop"] == ["ALL"]
    assert cli.get("healthcheck", {}).get("disable") is True


def test_the_receipts_dir_is_documented() -> None:
    lines = read_text(".env.example").splitlines()
    (line,) = [line for line in lines if re.match(r"^#?\s*PAPER_TRAIL_RECEIPTS_DIR=", line)]
    assert line == "#PAPER_TRAIL_RECEIPTS_DIR=/path/to/your/receipts"


def _stage(dockerfile: str, stage: str) -> str:
    match = re.search(
        rf"^FROM\s+\S+\s+AS\s+{re.escape(stage)}\s*$(.*?)(?=^FROM\s|\Z)",
        dockerfile,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    assert match, stage
    return match.group(1)


@pytest.mark.parametrize(
    ("image", "stages"),
    [
        ("runtime", ("python-base", "runtime")),
        ("dev", ("python-base", "python-deps", "python-dev")),
    ],
)
def test_tesseract_is_installed_in_the_runtime_and_dev_images(
    image: str, stages: tuple[str, ...]
) -> None:
    """ADR-0010: Tesseract + English data in the runtime image (and the dev image, where the
    integration tests run it), without recommends, apt lists removed in the same layer."""
    dockerfile = read_text("Dockerfile")
    body = "\n".join(_stage(dockerfile, stage) for stage in stages)

    installs = [
        command
        for command in re.split(r"\n(?=RUN )", body)
        if "apt-get install" in command and "tesseract-ocr" in command
    ]
    assert installs, f"tesseract is not installed in the {image} image"
    for package in TESSERACT_PACKAGES:
        assert re.search(rf"\b{package}\b", installs[0]), package
    assert "--no-install-recommends" in installs[0]
    assert "rm -rf /var/lib/apt/lists" in installs[0]


def _names(requirements: list[str]) -> dict[str, str]:
    return {re.split(r"[\s<>=!~;\[]", spec, maxsplit=1)[0].lower(): spec for spec in requirements}


def test_agent_core_dependencies() -> None:
    """Plan 2.1: LangGraph + its SQLite checkpointer, httpx, Pillow + HEIF at runtime; respx
    for the adapter tests. Each with a lower bound."""
    pyproject = tomllib.loads(read_text("pyproject.toml"))
    runtime = _names(pyproject["project"]["dependencies"])
    dev = _names(pyproject["dependency-groups"]["dev"])

    for name in RUNTIME_DEPENDENCIES:
        assert name in runtime, name
        assert ">=" in runtime[name], name
    assert "respx" in dev
    lock = read_text("uv.lock")
    for name in (*RUNTIME_DEPENDENCIES, "respx"):
        assert f'name = "{name}"' in lock, f"{name} is not locked (make lock)"
