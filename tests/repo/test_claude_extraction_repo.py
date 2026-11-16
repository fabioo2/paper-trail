"""Repository side of "Claude reads receipts, no GPU" (claude-extraction plan PR 1).

Covers CE-TC-061, 062, 064, 065, 067, 069, 070, 071, 073, 074; the compose file is
``test_compose.py`` (CE-TC-068), the Dockerfile ``test_dockerfile.py`` (063) and the Makefile
``test_makefile.py`` (072).

The import scans read every ``.py`` file with ``ast`` (relative imports resolved), and each is
shown to catch a planted import in a copy of the tree.
"""

import ast
import importlib.util
import json
import re
import shutil
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from paper_trail.bootstrap.agents import receipt_graph_mermaid
from paper_trail.config import Settings
from paper_trail.domain.enums import ProcessingStage
from paper_trail.domain.rules.arithmetic import IssueCode
from tests.repo.conftest import read_text
from tests.support import REPO_ROOT

# CE-TC-061 and 065: the local stack and the fakes that stood in for it.
DELETED_MODULES = (
    "pytesseract",
    "ollama",
    "paper_trail.adapters.tesseract",
    "paper_trail.adapters.ollama",
    "paper_trail.ports.ocr",
    "paper_trail.ports.embeddings",
    "paper_trail.adapters.sqlite.vector_index",
    "paper_trail.agents.routing",
    "paper_trail.agents.nodes.read",
    "tests.fakes.ollama_server",
    "tests.fakes.ocr",
    "tests.fakes.embeddings",
    "tests.fakes.llm",
    "tests.ollama_wire",
)
DELETED_PATHS = (
    "src/paper_trail/adapters/tesseract",
    "src/paper_trail/adapters/ollama",
    "src/paper_trail/ports/ocr.py",
    "src/paper_trail/ports/embeddings.py",
    "src/paper_trail/adapters/sqlite/vector_index.py",
    "src/paper_trail/agents/routing.py",
    "src/paper_trail/agents/nodes/read.py",
    "src/paper_trail/agents/prompts/vision",
    "tests/fakes/ollama_server.py",
    "tests/fakes/ocr.py",
    "tests/fakes/embeddings.py",
    "tests/fakes/llm.py",
    "tests/ollama_wire.py",
    "deploy/llm",
    "deploy/single-host",
)
SCANNED = ("src", "tests", "migrations")
# CE-TC-062: the one module that loads sqlite-vec (for migrations 0002 and 0007).
SQLITE_VEC_LOADER = ("src/paper_trail/adapters/sqlite/migrations.py",)
# CE-TC-064: only the Anthropic adapter talks to the network (S0-AC-38 egress as well).
# The Claude API and the product databases (claude-extraction plan D3, CE-TC-260).
NETWORK_ADAPTERS = ("src/paper_trail/adapters/anthropic", "src/paper_trail/adapters/lookups")
NETWORK_MODULES = (
    "anthropic",
    "httpx",
    "httpx2",
    "requests",
    "aiohttp",
    "urllib3",
    "urllib.request",
    "http.client",
)
LLM_DEPLOY_PATHS = ("deploy/llm", "deploy/single-host")
GRAPH_NODES = (
    "extract",
    "validate",
    "supervisor",
    "review",
    "persist",
    "match",
    "commit_matches",
    "enrich",
    "commit_enrichment",
)


def _imports(path: Path, root: Path) -> Iterator[str]:
    """Every module ``path`` imports, absolute, with ``from x import y`` also as ``x.y``."""
    package = ".".join(path.relative_to(root).parent.parts)
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            relative = "." * node.level + (node.module or "")
            module = importlib.util.resolve_name(relative, package) if node.level else relative
            yield module
            yield from (f"{module}.{alias.name}" for alias in node.names)


def _matches(module: str, banned: str) -> bool:
    return module == banned or module.startswith(f"{banned}.")


def _python_files(root: Path, folders: tuple[str, ...]) -> Iterator[Path]:
    for folder in folders:
        yield from sorted((root / folder).rglob("*.py"))


def _import_root(path: Path, root: Path) -> Path:
    """The directory imports are resolved from: ``src`` for the package, the repo otherwise."""
    return root / "src" if path.is_relative_to(root / "src") else root


def _offenders(
    root: Path,
    banned: tuple[str, ...],
    *,
    allowed: tuple[str, ...] = (),
    folders: tuple[str, ...] = SCANNED,
) -> list[str]:
    """``<file>: <module>`` for each import of a ``banned`` module outside ``allowed``."""
    return sorted(
        {
            f"{path.relative_to(root)}: {module}"
            for path in _python_files(root, folders)
            if not any(path.is_relative_to(root / place) for place in allowed)
            for module in _imports(path, _import_root(path, root))
            for name in banned
            if _matches(module, name)
        }
    )


def _planted_in(root: Path, banned: tuple[str, ...], **scope: Any) -> set[str]:
    """The files of the offences in the planted copy ``root`` that the real tree does not have
    (the scan must report the plant, whatever else is still being deleted)."""
    before = set(_offenders(REPO_ROOT, banned, **scope))
    return {line.split(": ", 1)[0] for line in set(_offenders(root, banned, **scope)) - before}


def _plant(tmp_path: Path, relative: str, statement: str) -> Path:
    """A copy of the source and test trees with ``statement`` appended to ``relative``."""
    root = tmp_path / "repo"
    for folder in SCANNED:
        shutil.copytree(
            REPO_ROOT / folder, root / folder, ignore=shutil.ignore_patterns("__pycache__")
        )
    with (root / relative).open("a", encoding="utf-8") as handle:
        handle.write(f"\n{statement}  # planted import\n")
    return root


def _lock_names() -> set[str]:
    return set(re.findall(r'^name = "([^"]+)"', read_text("uv.lock"), re.MULTILINE))


def _dependencies() -> dict[str, str]:
    requirements = tomllib.loads(read_text("pyproject.toml"))["project"]["dependencies"]
    return {re.split(r"[\s\[<>=!~;]", spec, maxsplit=1)[0].lower(): spec for spec in requirements}


# --- CE-TC-061, 065: nothing imports the deleted stack ------------------------------------------


def test_ce_tc_061_nothing_imports_the_deleted_stack() -> None:
    assert _offenders(REPO_ROOT, DELETED_MODULES) == []


@pytest.mark.parametrize("path", DELETED_PATHS)
def test_ce_tc_061_065_067_the_deleted_paths_are_gone(path: str) -> None:
    assert not (REPO_ROOT / path).exists()


def test_ce_tc_061_pytesseract_is_no_dependency() -> None:
    assert "pytesseract" not in _dependencies()
    assert "pytesseract" not in _lock_names()


@pytest.mark.parametrize(
    ("relative", "statement"),
    [
        ("src/paper_trail/domain/money.py", "import pytesseract"),
        ("src/paper_trail/agents/graph.py", "from .routing import effective_limits"),
        ("tests/support.py", "from tests.fakes.ollama_server import closed_loopback_url"),
    ],
)
def test_ce_tc_061_the_scan_reports_a_planted_import(
    tmp_path: Path, relative: str, statement: str
) -> None:
    root = _plant(tmp_path, relative, statement)

    assert _planted_in(root, DELETED_MODULES) == {relative}


# --- CE-TC-062: sqlite-vec is loaded only by the migration runner -------------------------------


def test_ce_tc_062_only_the_migration_runner_imports_sqlite_vec() -> None:
    assert _offenders(REPO_ROOT, ("sqlite_vec",), allowed=SQLITE_VEC_LOADER) == []
    assert "sqlite_vec" in set(_imports(REPO_ROOT / SQLITE_VEC_LOADER[0], REPO_ROOT / "src")), (
        "the migration runner loads it for 0002 and 0007"
    )


@pytest.mark.parametrize(
    ("relative", "statement"),
    [
        ("src/paper_trail/adapters/sqlite/engine.py", "import sqlite_vec"),
        ("src/paper_trail/adapters/sqlite/health.py", "from sqlite_vec import load"),
        ("migrations/env.py", "import sqlite_vec"),
    ],
)
def test_ce_tc_062_the_scan_reports_a_planted_import(
    tmp_path: Path, relative: str, statement: str
) -> None:
    root = _plant(tmp_path, relative, statement)

    assert _planted_in(root, ("sqlite_vec",), allowed=SQLITE_VEC_LOADER) == {relative}


def test_ce_tc_062_sqlite_vec_stays_a_dependency() -> None:
    assert "sqlite-vec" in _dependencies()
    assert "sqlite-vec" in _lock_names()


# --- CE-TC-064: only the Anthropic adapter reaches the network ----------------------------------


def test_ce_tc_064_only_the_network_adapters_import_a_network_client() -> None:
    """Only the Anthropic and the lookups adapters may; ``tests`` may too (the canned SDK
    clients and loopback fakes)."""
    offenders = _offenders(
        REPO_ROOT, NETWORK_MODULES, allowed=NETWORK_ADAPTERS, folders=("src", "migrations")
    )

    assert offenders == []


@pytest.mark.parametrize(
    ("relative", "statement"),
    [
        ("src/paper_trail/agents/nodes/extraction.py", "import anthropic"),
        ("src/paper_trail/application/budget.py", "from anthropic import Anthropic"),
        ("src/paper_trail/bootstrap/agents.py", "import httpx"),
        ("src/paper_trail/application/enrichment.py", "import httpx2"),
        ("src/paper_trail/adapters/catalog/plu.py", "from urllib.request import urlopen"),
    ],
)
def test_ce_tc_064_the_scan_reports_a_planted_import(
    tmp_path: Path, relative: str, statement: str
) -> None:
    root = _plant(tmp_path, relative, statement)

    planted = _planted_in(
        root, NETWORK_MODULES, allowed=NETWORK_ADAPTERS, folders=("src", "migrations")
    )

    assert planted == {relative}


def test_ce_tc_064_the_sdk_is_a_locked_dependency() -> None:
    assert ">=" in _dependencies()["anthropic"]
    assert "anthropic" in _lock_names()


# --- CE-TC-067: nothing refers to the LLM deploy folders ----------------------------------------


def _deploy_references() -> Iterator[Path]:
    yield from (REPO_ROOT / name for name in ("Makefile", "README.md", "CONTRIBUTING.md"))
    yield from sorted(REPO_ROOT.glob("compose*.yaml"))
    yield from sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("path", list(_deploy_references()), ids=lambda path: path.name)
def test_ce_tc_067_no_file_refers_to_the_llm_deploy_folders(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert [folder for folder in LLM_DEPLOY_PATHS if folder in text] == []


# --- CE-TC-069: .env.example ---------------------------------------------------------------------


def test_ce_tc_069_env_example_documents_the_key_empty_and_the_budget() -> None:
    lines = read_text(".env.example").splitlines()

    assert "ANTHROPIC_API_KEY=" in lines
    assert [
        line for line in lines if re.match(r"^#?\s*PAPER_TRAIL_CLAUDE__MONTHLY_BUDGET_USD=", line)
    ]
    assert [line for line in lines if re.search(r"OLLAMA|_OCR__", line)] == []


# --- CE-TC-070: the README says what the plan requires ------------------------------------------

README_REQUIRES = (
    "ANTHROPIC_API_KEY",
    "PAPER_TRAIL_CLAUDE__MONTHLY_BUDGET_USD",
    # The upgrade note lists the settings that went (they are ignored if still set, CQ7).
    "PAPER_TRAIL_OCR__",
    "PAPER_TRAIL_OLLAMA__",
    "PAPER_TRAIL_MATCHING__",
    "GRAPH__MAX_TEXT_ATTEMPTS",
    "GRAPH__MAX_VISION_ATTEMPTS",
    "GRAPH__VISION_FIRST_BELOW",
    "GRAPH__MIN_CONFIDENCE",
    "PROMPTS__VISION_VERSION",
)
README_PHRASES = ("back up the database", "resolve open reviews", "failed runs")
# Case-sensitive: the removed variables' names (OLLAMA, OCR) are listed on purpose.
README_FORBIDS = ("GPU", "Ollama", "Tesseract", "AI stays local")
# R1, V4: what the photo sent to Anthropic can show, said in one paragraph.
PRIVACY_WORDS = ("anthropic", "photo", "card", "loyalty", "name")


@pytest.fixture(scope="module")
def readme() -> str:
    return read_text("README.md")


@pytest.mark.parametrize("needle", README_REQUIRES)
def test_ce_tc_070_readme_names_the_settings(readme: str, needle: str) -> None:
    assert needle in readme


@pytest.mark.parametrize("phrase", README_PHRASES)
def test_ce_tc_070_readme_has_the_upgrade_notes(readme: str, phrase: str) -> None:
    assert phrase in readme.lower()


@pytest.mark.parametrize("word", README_FORBIDS)
def test_ce_tc_070_readme_no_longer_mentions_the_local_stack(readme: str, word: str) -> None:
    assert word not in readme


def test_ce_tc_070_readme_says_what_the_photo_sent_to_anthropic_shows(readme: str) -> None:
    paragraphs = [paragraph.lower() for paragraph in re.split(r"\n\s*\n", readme)]

    assert [p for p in paragraphs if all(word in p for word in PRIVACY_WORDS)]


# --- CE-TC-071: ADRs ------------------------------------------------------------------------------


def _adr(number: str) -> str:
    (path,) = sorted((REPO_ROOT / "docs" / "adr").glob(f"{number}-*.md"))
    return path.read_text(encoding="utf-8")


def _status(adr: str) -> str:
    return adr.split("## Status", 1)[1].split("\n## ", 1)[0].strip()


@pytest.mark.parametrize("number", ["0010", "0016"])
def test_ce_tc_071_the_local_stack_adrs_are_superseded(number: str) -> None:
    assert re.match(r"Superseded by \[?ADR-0019\b", _status(_adr(number)))


@pytest.mark.parametrize("number", ["0017", "0019"])
def test_ce_tc_071_adr_0017_stays_and_0019_is_accepted(number: str) -> None:
    assert _status(_adr(number)) == "Accepted"


# --- CE-TC-073: the committed graph diagram is the graph ----------------------------------------


def _mermaid_blocks(text: str) -> list[str]:
    return [block.strip() for block in re.findall(r"```mermaid\n(.*?)```", text, re.DOTALL)]


def test_ce_tc_073_the_committed_graph_diagram_matches_the_graph(tmp_path: Path) -> None:
    documents = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").rglob("*.md"))]
    diagrams = [
        block
        for document in documents
        for block in _mermaid_blocks(document.read_text(encoding="utf-8"))
        if "graph TD" in block
    ]
    drawn = receipt_graph_mermaid(Settings(env="test", data_dir=tmp_path)).strip()

    assert diagrams, "the receipt graph is committed as a Mermaid block"
    assert diagrams[0] == drawn
    assert [node for node in GRAPH_NODES if node not in drawn] == []
    assert "extract_vision" not in drawn
    assert "extract_text" not in drawn


# --- CE-TC-074: the committed API contract follows the stage and issue enums ---------------------


@pytest.mark.parametrize(
    ("schema", "values"),
    [
        ("ProcessingStage", [stage.value for stage in ProcessingStage]),
        ("IssueCode", [code.value for code in IssueCode]),
    ],
)
def test_ce_tc_074_the_committed_spec_has_the_backend_enums(schema: str, values: list[str]) -> None:
    spec = json.loads(read_text("frontend/openapi.json"))

    assert spec["components"]["schemas"][schema]["enum"] == values
    assert "waiting_for_gpu" not in read_text("frontend/src/api/schema.d.ts")
