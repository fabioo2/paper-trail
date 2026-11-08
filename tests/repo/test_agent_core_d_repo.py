"""Repository side of agent core slice D: the parallel test workers are capped.

Pinned (docs/tests/agent-core-D.md): every pytest-xdist run in the Makefile uses at most 6
workers by default, set in one place (``PYTEST_WORKERS``), never a hard-coded ``-n auto``: the
host's 8 cores also run other self-hosted services. ``PYTEST_WORKERS`` is a ``?=`` default so CI
can set it to ``auto`` (the 2-vCPU GitHub runner: six workers oversubscribed it, and a Tesseract
run timed out; first public CI run, 2026-11-08).
"""

import re
from typing import Any

from tests.repo.conftest import load_yaml, read_text
from tests.repo.makefile_parser import Makefile

MAX_WORKERS = 6
_WORKERS = re.compile(r"(?:^|\s)(?:-n|--numprocesses)[\s=](\S+)")


def test_parallel_test_runs_use_at_most_six_workers(makefile: Makefile) -> None:
    runs = [
        (target, match.group(1))
        for target, rule in makefile.rules.items()
        for command in rule.commands
        if "pytest" in command
        for match in _WORKERS.finditer(command)
    ]

    assert {target for target, _ in runs} >= {"test", "test-fast"}
    assert {workers for _, workers in runs} == {"$(PYTEST_WORKERS)"}, runs
    assert 1 <= int(makefile.variables["PYTEST_WORKERS"]) <= MAX_WORKERS


def test_the_worker_count_is_a_default_the_environment_can_override() -> None:
    assert re.search(r"^PYTEST_WORKERS \?= \d+$", read_text("Makefile"), re.MULTILINE)


def test_ci_runs_the_tests_with_one_worker_per_cpu_of_its_runner() -> None:
    workflow: dict[str, Any] = load_yaml(".github/workflows/ci.yml")
    (step,) = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "make lint typecheck test" in str(step.get("run", ""))
    ]

    assert step["env"]["PYTEST_WORKERS"] == "auto"
