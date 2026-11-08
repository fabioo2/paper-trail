"""Migrations are frozen history (S1-AC-09, PLAN 3.5): they never import application code.

CHECK constraints for enums are written into the migration as literal value lists, so changing
an enum later cannot silently change what an old migration creates.
"""

import ast
from pathlib import Path

import pytest

from tests.support import REPO_ROOT

VERSIONS = sorted((REPO_ROOT / "migrations" / "versions").glob("*.py"))


def _imported_packages(tree: ast.Module) -> set[str]:
    packages = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            packages |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            packages.add((node.module or "").split(".")[0])
    return packages


@pytest.mark.parametrize("migration", VERSIONS, ids=lambda path: path.name)
def test_s1_ac_09_migrations_do_not_import_the_application(migration: Path) -> None:
    """PLAN 3.5: a migration imports alembic, sqlalchemy and the standard library only."""
    tree = ast.parse(migration.read_text(encoding="utf-8"))

    assert "paper_trail" not in _imported_packages(tree)


def _revision_ids(filename: str) -> tuple[object, object]:
    """(revision, down_revision) as literally assigned in ``migrations/versions/<filename>``."""
    tree = ast.parse((REPO_ROOT / "migrations" / "versions" / filename).read_text(encoding="utf-8"))
    assignments = {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name) and node.value is not None
    }
    return (
        ast.literal_eval(assignments["revision"]),
        ast.literal_eval(assignments["down_revision"]),
    )


def test_s1_ac_09_core_schema_is_revision_0002_after_0001() -> None:
    """S1-AC-09: migrations/versions/0002_core_schema.py is revision "0002", revising "0001"."""
    assert _revision_ids("0002_core_schema.py") == ("0002", "0001")


def test_ac_a_agent_core_schema_is_revision_0003_after_0002() -> None:
    """Agent core (PLAN 3.5): migrations/versions/0003_agent_core.py is revision "0003",
    revising "0002" (Stage 2's ingestion migration becomes 0004)."""
    assert _revision_ids("0003_agent_core.py") == ("0003", "0002")
