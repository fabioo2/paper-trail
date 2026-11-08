"""Layering contracts are enforced (S0-AC-06, PLAN 1.6).

import-linter enforces the layer-to-layer rules: each case copies the real package into a
temporary directory, plants exactly one forbidden import and runs ``lint-imports`` with the
project's real configuration against that copy. import-linter can only forbid packages it is
told about, so "domain and ports use the standard library only" (rules 1 and 2) is enforced here
generically: an AST scan allows ``sys.stdlib_module_names`` and the inner layers, nothing else.
"""

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support import REPO_ROOT

LINT_IMPORTS = Path(sys.executable).with_name("lint-imports")

# (importer module, forbidden import, PLAN 1.6 rule)
VIOLATIONS = [
    ("paper_trail.domain.health", "paper_trail.adapters", "1: domain imports only domain"),
    ("paper_trail.domain.health", "paper_trail.config", "1: domain imports only domain"),
    ("paper_trail.domain.health", "sqlalchemy", "1: domain imports no third-party packages"),
    ("paper_trail.domain.health", "pydantic", "1: domain imports no third-party packages"),
    ("paper_trail.ports.clock", "paper_trail.adapters", "2: ports import only domain"),
    ("paper_trail.ports.clock", "paper_trail.application", "2: ports import only domain"),
    ("paper_trail.application.health", "paper_trail.adapters", "3: application not adapters"),
    ("paper_trail.application.health", "paper_trail.interfaces", "3: application not interfaces"),
    ("paper_trail.application.health", "paper_trail.bootstrap", "3: application not bootstrap"),
    ("paper_trail.agents", "paper_trail.adapters", "4: agents not adapters"),
    ("paper_trail.tools", "paper_trail.interfaces", "4: tools not interfaces"),
    ("paper_trail.adapters.clock", "paper_trail.interfaces", "5: adapters not interfaces"),
    ("paper_trail.adapters.clock", "paper_trail.application", "5: adapters not application"),
    ("paper_trail.interfaces.web.app", "paper_trail.adapters", "6: interfaces not adapters"),
    ("paper_trail.adapters.clock", "paper_trail.bootstrap", "7: only interfaces import bootstrap"),
    ("paper_trail.observability", "paper_trail.adapters", "7: only bootstrap imports adapters"),
    # S5-AC-06 (agent core slice B): agents reach storage only through their tools.
    ("paper_trail.agents.graph", "paper_trail.ports.repositories", "S5-AC-06: no repository port"),
    ("paper_trail.agents.nodes.persist", "paper_trail.ports.unit_of_work", "S5-AC-06: no UoW"),
    ("paper_trail.agents.runner", "paper_trail.ports.queues", "S5-AC-06: no queue port"),
    ("paper_trail.agents.prompts", "paper_trail.ports.search", "S5-AC-06: no search port"),
    ("paper_trail.agents.nodes.read", "paper_trail.adapters.mcp", "S5-AC-06: no adapter"),
    ("paper_trail.adapters.mcp", "paper_trail.interfaces", "5: adapters not interfaces"),
    ("paper_trail.interfaces.mcp", "paper_trail.adapters", "6: interfaces not adapters"),
]


def _run_lint_imports(project: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(project / "src")}
    return subprocess.run(  # noqa: S603 - fixed argv: the venv's lint-imports script
        [str(LINT_IMPORTS), "--config", str(project / "pyproject.toml"), "--no-cache"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture
def project_copy(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(
        REPO_ROOT / "src", project / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy2(REPO_ROOT / "pyproject.toml", project / "pyproject.toml")
    return project


def _module_file(project: Path, module: str) -> Path:
    base = project / "src" / Path(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    package = base / "__init__.py"
    package.parent.mkdir(parents=True, exist_ok=True)  # agents/tools arrive in later stages
    package.touch()
    return package


def test_s0_ac_06_real_tree_satisfies_every_contract(project_copy: Path) -> None:
    """S0-AC-06: the unmodified source tree passes lint-imports."""
    result = _run_lint_imports(project_copy)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "BROKEN" not in result.stdout


@pytest.mark.parametrize(("importer", "imported", "rule"), VIOLATIONS)
def test_s0_ac_06_forbidden_import_is_reported(
    project_copy: Path, importer: str, imported: str, rule: str
) -> None:
    """S0-AC-06: a planted forbidden import breaks a contract and fails lint-imports."""
    target = _module_file(project_copy, importer)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"\nimport {imported}  # planted layering violation\n")

    result = _run_lint_imports(project_copy)

    assert result.returncode != 0, f"rule {rule!r} not enforced:\n{result.stdout}"
    assert "BROKEN" in result.stdout
    assert importer in result.stdout


# --- rules 1 and 2: domain and ports import the standard library and inner layers only --------

STDLIB = frozenset(sys.stdlib_module_names)
# layer -> the paper_trail packages it may import (PLAN 1.6 rules 1 and 2)
PURE_LAYERS = {
    "domain": ("paper_trail.domain",),
    "ports": ("paper_trail.domain", "paper_trail.ports"),
}


def _imports(path: Path, src: Path) -> Iterator[str]:
    """Absolute names of every module ``path`` imports, with relative imports resolved."""
    # The package a relative import starts from: the containing package, also for __init__.py.
    package = ".".join(path.relative_to(src).parent.parts)
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            relative = "." * node.level + (node.module or "")
            yield importlib.util.resolve_name(relative, package) if node.level else relative


def _is_allowed(module: str, layers: tuple[str, ...]) -> bool:
    if module.partition(".")[0] in STDLIB:
        return True
    return any(module == layer or module.startswith(f"{layer}.") for layer in layers)


def _pure_layer_violations(src: Path) -> list[str]:
    return [
        f"{path.relative_to(src)}: {module}"
        for layer, allowed in PURE_LAYERS.items()
        for path in sorted((src / "paper_trail" / layer).rglob("*.py"))
        for module in _imports(path, src)
        if not _is_allowed(module, allowed)
    ]


def test_s0_ac_06_domain_and_ports_import_only_stdlib_and_inner_layers() -> None:
    """S0-AC-06 / PLAN 1.6 rules 1-2: no third-party package at all, listed or not."""
    assert _pure_layer_violations(REPO_ROOT / "src") == []


@pytest.mark.parametrize(
    ("module", "statement"),
    [
        ("domain/health.py", "import httpx"),
        ("domain/health.py", "from numpy import ndarray"),
        ("domain/health.py", "import langgraph.graph"),
        ("domain/health.py", "from .. import config"),
        ("domain/health.py", "from paper_trail.ports import clock"),
        ("domain/__init__.py", "from ..adapters import clock"),
        ("ports/clock.py", "import pydantic"),
        ("ports/clock.py", "from ..application import health"),
    ],
)
def test_s0_ac_06_unlisted_third_party_import_is_reported(
    project_copy: Path, module: str, statement: str
) -> None:
    """S0-AC-06: a planted import of any non-stdlib, outer-layer module is caught."""
    target = project_copy / "src" / "paper_trail" / module
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"\n{statement}  # planted layering violation\n")

    violations = _pure_layer_violations(project_copy / "src")

    assert len(violations) == 1, violations
    assert violations[0].startswith(f"paper_trail/{module}: ")


def test_s0_ac_06_stdlib_and_inner_layer_imports_are_allowed(project_copy: Path) -> None:
    """S0-AC-06: the scan does not flag the standard library or the layer's own packages."""
    target = project_copy / "src" / "paper_trail" / "ports" / "clock.py"
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\nimport collections.abc\nfrom typing import Protocol\n")
        handle.write("from paper_trail.domain import health\nfrom . import health as h\n")

    assert _pure_layer_violations(project_copy / "src") == []
