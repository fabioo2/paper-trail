"""Versioned prompt files and their rendering (agent-core plan 2.5, S3-AC-09).

A prompt lives in ``prompts/<name>/<version>/``: ``system.md`` and the ``user.md`` template,
sent after the receipt photo. Validation feedback goes inside ``<validation_feedback>``; the
system prompt says what is there quotes the previous answer. Feedback is untrusted (it quotes
the model's reading of the receipt), so ``neutralize_delimiters`` escapes the data-block tags
in it first: a ``</validation_feedback>`` printed on a receipt cannot close the block early
(agent core slice B).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"
_PLACEHOLDER = re.compile(r"\{feedback_block\}")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# An opening or closing data-block tag, however spaced, cased or attributed.
_DELIMITER = re.compile(r"<\s*/?\s*(?:receipt_data|validation_feedback)\b[^<>]*>", re.IGNORECASE)


def neutralize_delimiters(text: str) -> str:
    """``text`` with every data-block tag's angle brackets escaped (``&lt;``, ``&gt;``) and
    nothing else changed. Repeats until no tag is left, because escaping an inner tag can
    complete an outer one (``<receipt_data<receipt_data>>``); idempotent."""
    while True:
        escaped = _DELIMITER.sub(_escape, text)
        if escaped == text:
            return text
        text = escaped


def _escape(match: re.Match[str]) -> str:
    return match.group(0).replace("<", "&lt;").replace(">", "&gt;")


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    tag: str  # "<name>/<version>"; recorded on every model call
    system: str
    user_template: str

    def text(self, feedback: Sequence[str]) -> str:
        """The user turn after the photo, with the validation feedback (one line per issue)
        in ``<validation_feedback>`` when there is any."""
        lines = "\n".join(neutralize_delimiters(line) for line in feedback)
        block = f"<validation_feedback>\n{lines}\n</validation_feedback>\n" if feedback else ""
        # One pass, so braces inside the feedback are never template fields.
        return _PLACEHOLDER.sub(lambda _: block, self.user_template).rstrip("\n")


class PromptLibrary:
    def __init__(self, root: Path = PROMPTS_DIR) -> None:
        self._root = root

    def load(self, name: str, version: str) -> RenderedPrompt:
        """The prompt ``name`` at ``version``; LookupError when there is no such prompt."""
        directory = self._root / name / version
        safe = _SEGMENT.fullmatch(name) and _SEGMENT.fullmatch(version)
        if not safe or not (directory / "system.md").is_file():
            msg = f"no prompt {name}/{version} in {self._root}"
            raise LookupError(msg)
        return RenderedPrompt(
            tag=f"{name}/{version}",
            system=(directory / "system.md").read_text(encoding="utf-8").strip(),
            user_template=(directory / "user.md").read_text(encoding="utf-8").rstrip("\n"),
        )
