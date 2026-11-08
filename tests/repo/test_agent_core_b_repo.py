"""Repository wiring of agent core slice B: the MCP service, its Make targets and dependency,
and the slice A review note that is about files (note 3).

Pinned (docs/tests/agent-core-B.md):
- Runtime dependency ``mcp`` with a lower bound and an upper bound below the next major
  (``>=2.3,<3``): 2.x renamed FastMCP to MCPServer, so the SDK's churn is fenced off; locked.
- Compose service ``mcp``: the app image, profile ``mcp``, ``command: ["mcp"]``, published on
  ``127.0.0.1:8421:8000`` only (literal loopback: the server has no authentication and can
  write), waits for ``migrate``, hardened like the runtime services, with memory and CPU limits.
- ``make mcp-up`` = ``$(DC) --profile mcp up -d mcp``; ``make mcp-down`` stops and removes only
  the ``mcp`` container (never ``down``, which would stop the whole stack).
- Review note 3: the ``+noex`` tag suffix is spelled in one source file, ``agents/prompts.py``.
"""

import re
import tomllib
from typing import Any

from tests.repo.conftest import read_text
from tests.repo.makefile_parser import Makefile
from tests.support import REPO_ROOT

APP_IMAGE = "paper-trail:${PAPER_TRAIL_IMAGE_TAG:-latest}"


def _recipe(makefile: Makefile, target: str) -> str:
    return "\n".join(makefile.rule(target).commands)


def test_the_mcp_sdk_is_a_fenced_runtime_dependency() -> None:
    pyproject = tomllib.loads(read_text("pyproject.toml"))
    specs = {
        re.split(r"[\s<>=!~;\[]", spec, maxsplit=1)[0].lower(): spec
        for spec in pyproject["project"]["dependencies"]
    }

    assert "mcp" in specs
    assert re.search(r">=\s*2\.3", specs["mcp"]), specs["mcp"]
    assert re.search(r"<\s*3", specs["mcp"]), specs["mcp"]
    assert 'name = "mcp"' in read_text("uv.lock")


def test_the_mcp_service(compose: dict[str, Any]) -> None:
    """PLAN 2.4 / 7: same image, profile mcp, loopback-only port 8421, after migrate."""
    mcp = compose["services"]["mcp"]

    assert mcp["image"] == APP_IMAGE
    assert mcp["profiles"] == ["mcp"]
    assert mcp["command"] == ["mcp"]
    assert mcp["ports"] == ["127.0.0.1:8421:8000"]
    assert mcp["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert mcp["read_only"] is True
    assert mcp["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in mcp["security_opt"]
    assert mcp["mem_limit"]
    assert float(mcp["cpus"]) > 0
    assert "${PAPER_TRAIL_DATA_VOLUME:-paper-trail-data}:/data" in mcp["volumes"]


def test_make_mcp_up_and_down(makefile: Makefile) -> None:
    for target in ("mcp-up", "mcp-down"):
        assert makefile.rule(target).help, target
        assert target in makefile.rule(".PHONY").prerequisites
    up = _recipe(makefile, "mcp-up")
    down = _recipe(makefile, "mcp-down")

    assert up == "$(DC) --profile mcp up -d mcp"
    assert down.startswith("$(DC) --profile mcp ")
    assert down.rstrip().endswith(" mcp")
    assert " down" not in down  # `down` would stop web and worker too


def test_review_note_3_the_no_examples_suffix_has_one_source() -> None:
    sources = sorted(
        path.relative_to(REPO_ROOT / "src").as_posix()
        for path in (REPO_ROOT / "src").rglob("*.py")
        if "+noex" in path.read_text(encoding="utf-8")
    )

    assert sources == ["paper_trail/agents/prompts.py"]
