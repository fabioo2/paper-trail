"""Repository side of agent core slice C: the slice B review note that is about files (note 3).

Pinned (docs/tests/agent-core-C.md):
- Review note 3: ``--transport`` is applied to ``Settings`` in one place of the CLI
  (``with_transport``), so exactly one ``tools.model_copy(`` exists under ``interfaces/cli``.
"""

import re

from tests.support import REPO_ROOT


def test_review_note_3_the_transport_override_has_one_home() -> None:
    cli = REPO_ROOT / "src" / "paper_trail" / "interfaces" / "cli"
    uses = [
        path.name
        for path in sorted(cli.glob("*.py"))
        for _ in re.finditer(r"tools\.model_copy\(", path.read_text(encoding="utf-8"))
    ]

    assert len(uses) == 1, uses
