"""No catalog module can reach the network (barcode-lookup PR 1a, R12; PC-TC-80).

A static scan of every module that parses, stores or looks up product codes: none imports an
HTTP client or ``socket``. The scan is shown to catch a planted import in a copy of the tree. The
only mention of ``costco.ca`` in ``src/`` is the search-link template ``STORE_SEARCH_URLS``
(built for a person to open, never requested). The runtime half is
``tests/unit/test_catalog_offline.py``.
"""

import ast
import shutil
from pathlib import Path

import pytest

from tests.support import REPO_ROOT

CATALOG_MODULES = (
    "domain/rules/item_codes.py",
    "ports/catalog.py",
    "adapters/catalog",
    "adapters/sqlite/store_catalog.py",
    "adapters/sqlite/product_code_repository.py",
    "application/catalog.py",
)
# PR 1b adds the CLI module; it joins the scan as soon as it exists.
LATER_MODULES = ("interfaces/cli/catalog.py",)
FORBIDDEN = ("httpx", "requests", "urllib.request", "urllib3", "http.client", "aiohttp", "socket")


def _files(src: Path) -> list[Path]:
    package = src / "paper_trail"
    files: list[Path] = []
    for relative in CATALOG_MODULES:
        path = package / relative
        assert path.exists(), relative
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    files.extend(path for path in (package / r for r in LATER_MODULES) if path.exists())
    return files


def _imported(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _network_imports(src: Path) -> list[str]:
    """``<file>: <forbidden module>`` for each forbidden module a catalog file imports."""
    return [
        f"{path.relative_to(src)}: {banned}"
        for path in _files(src)
        for banned in FORBIDDEN
        if any(name == banned or name.startswith(f"{banned}.") for name in _imported(path))
    ]


def test_no_catalog_module_imports_a_network_client() -> None:
    assert _network_imports(REPO_ROOT / "src") == []


@pytest.mark.parametrize(
    ("module", "statement"),
    [
        ("adapters/catalog/plu.py", "import httpx"),
        ("domain/rules/item_codes.py", "from urllib.request import urlopen"),
        ("adapters/sqlite/store_catalog.py", "import socket"),
        ("application/catalog.py", "from http import client"),
    ],
)
def test_the_scan_reports_a_planted_import(tmp_path: Path, module: str, statement: str) -> None:
    src = tmp_path / "src"
    shutil.copytree(REPO_ROOT / "src", src, ignore=shutil.ignore_patterns("__pycache__"))
    with (src / "paper_trail" / module).open("a", encoding="utf-8") as handle:
        handle.write(f"\n{statement}  # planted network import\n")

    violations = _network_imports(src)

    assert len(violations) == 1, violations
    assert violations[0].startswith(f"paper_trail/{module}: ")


def test_costco_ca_appears_only_in_the_search_link_template() -> None:
    src = REPO_ROOT / "src"
    mentions = [
        f"{path.relative_to(src)}: {line.strip()}"
        for path in sorted(src.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".json", ".md", ".txt", ".toml", ".yaml"}
        for line in path.read_text(encoding="utf-8").splitlines()
        if "costco.ca" in line
    ]

    assert mentions == [
        'paper_trail/domain/rules/item_codes.py: STORE_SEARCH_URLS = {"costco": '
        '"https://www.costco.ca/CatalogSearch?keyword={code}"}'
    ]
