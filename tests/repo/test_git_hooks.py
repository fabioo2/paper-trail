"""Git hooks run the containerized pre-commit (S0-AC-35)."""

import os
import stat

from tests.repo.conftest import load_yaml
from tests.support import REPO_ROOT

HOOK = REPO_ROOT / ".githooks" / "pre-commit"
REQUIRED_HOOK_COMMANDS = (
    "ruff check",
    "ruff format --check",
    "lint-imports",
    "check-yaml",
    "check-toml",
    "end-of-file-fixer",
    "trailing-whitespace",
    "check-added-large-files",
    "detect-private-key",
)


def test_s0_ac_35_pre_commit_hook_is_executable() -> None:
    """S0-AC-35: .githooks/pre-commit exists and is executable."""
    assert HOOK.is_file()
    assert HOOK.stat().st_mode & stat.S_IXUSR
    assert os.access(HOOK, os.X_OK)


def test_s0_ac_35_pre_commit_hook_runs_make_pre_commit() -> None:
    """S0-AC-35: the hook is POSIX sh and execs `make pre-commit`."""
    lines = HOOK.read_text(encoding="utf-8").splitlines()

    assert lines[0] == "#!/bin/sh"
    assert "exec make pre-commit" in [line.strip() for line in lines]


def test_s0_ac_35_pre_commit_config_uses_local_system_hooks_only() -> None:
    """S0-AC-35 / PLAN 2.7: only `repo: local`, `language: system` hooks (run inside dev)."""
    config = load_yaml(".pre-commit-config.yaml")
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]

    assert {repo["repo"] for repo in config["repos"]} == {"local"}
    assert {hook["language"] for hook in hooks} == {"system"}
    entries = " ".join(hook["entry"] for hook in hooks)
    for command in REQUIRED_HOOK_COMMANDS:
        assert command in entries, f"pre-commit does not run `{command}`"


def test_s0_ac_35_large_file_limit_is_500_kb() -> None:
    """S0-AC-35 / PLAN 2.7: check-added-large-files rejects files over 500 kB."""
    config = load_yaml(".pre-commit-config.yaml")
    hooks = [h for r in config["repos"] for h in r["hooks"]]
    (hook,) = [h for h in hooks if "check-added-large-files" in h["entry"]]

    assert "--maxkb=500" in " ".join([hook.get("entry", ""), *hook.get("args", [])])
