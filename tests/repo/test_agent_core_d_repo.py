"""Repository side of agent core slice D: the parallel test workers are capped.

Pinned (docs/tests/agent-core-D.md): every pytest-xdist run in the Makefile uses at most 6
workers, set in one place (``PYTEST_WORKERS``), never ``-n auto``: the host's 8 cores also run
the owner's media services. CI calls the Make targets, so it inherits the cap.
"""

import re

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
