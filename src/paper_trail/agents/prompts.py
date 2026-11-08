"""Versioned prompt files and their rendering (agent-core plan 2.5, S3-AC-09).

A prompt lives in ``prompts/<name>/<version>/``: ``system.md``, the ``user.md`` template, the
model's ``schema.json`` and few-shot ``examples/*.json`` (``{"ocr_text", "output"}``), rendered
as user/assistant pairs. Receipt text goes inside ``<receipt_data>`` and validation feedback
inside ``<validation_feedback>``; the system prompt says that content there is data, never
instructions. Both are untrusted (feedback quotes the model's reading of the receipt), so
``neutralize_delimiters`` escapes those tags in them first: a ``</receipt_data>`` printed on a
receipt cannot close the data block early (agent core slice B).
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paper_trail.ports.llm import ChatMessage

PROMPTS_DIR = Path(__file__).parent / "prompts"
_PLACEHOLDER = re.compile(r"\{(feedback_block|ocr_text)\}")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_NO_EXAMPLES = "+noex"
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
    tag: str  # "<name>/<version>", plus "+noex" without examples; recorded on every LLM call
    system: str
    user_template: str
    examples: tuple[tuple[str, Mapping[str, Any]], ...]  # (ocr_text, output)

    def messages(
        self, ocr_text: str, feedback: Sequence[str], images: Sequence[bytes] = ()
    ) -> tuple[ChatMessage, ...]:
        """System, one (user, assistant) pair per example, then this receipt's user message
        with the validation feedback (one line per issue) ahead of the receipt data, and the
        ``images`` (a vision model's photo) attached to that last message only."""
        shots = [
            message
            for example_text, output in self.examples
            for message in (
                ChatMessage("user", self._user(example_text, ())),
                ChatMessage("assistant", json.dumps(output, separators=(",", ":"))),
            )
        ]
        return (
            ChatMessage("system", self.system),
            *shots,
            ChatMessage("user", self._user(ocr_text, feedback), tuple(images)),
        )

    def example_texts(self) -> frozenset[str]:
        """The store names and line raw texts of the example answers (any extraction schema
        version has both): what an answer must not copy (ADR-0017's leak guard)."""
        return frozenset(
            text
            for _, output in self.examples
            for text in (output["store_name"], *(line["raw_text"] for line in output["items"]))
        )

    def _user(self, ocr_text: str, feedback: Sequence[str]) -> str:
        lines = "\n".join(neutralize_delimiters(line) for line in feedback)
        block = f"<validation_feedback>\n{lines}\n</validation_feedback>\n" if feedback else ""
        values = {"feedback_block": block, "ocr_text": neutralize_delimiters(ocr_text)}
        # One pass, so braces inside the receipt or the feedback are never template fields.
        return _PLACEHOLDER.sub(lambda match: values[match.group(1)], self.user_template)


class PromptLibrary:
    def __init__(self, root: Path = PROMPTS_DIR) -> None:
        self._root = root

    def load(self, name: str, version: str, *, examples: bool = True) -> RenderedPrompt:
        """The prompt ``name`` at ``version``; LookupError when there is no such prompt."""
        directory = self._root / name / version
        safe = _SEGMENT.fullmatch(name) and _SEGMENT.fullmatch(version)
        if not safe or not (directory / "system.md").is_file():
            msg = f"no prompt {name}/{version} in {self._root}"
            raise LookupError(msg)
        shots = (
            tuple(
                _example(json.loads(path.read_text(encoding="utf-8")))
                for path in sorted((directory / "examples").glob("*.json"))
            )
            if examples
            else ()
        )
        return RenderedPrompt(
            tag=f"{name}/{version}" + ("" if examples else _NO_EXAMPLES),
            system=(directory / "system.md").read_text(encoding="utf-8").strip(),
            user_template=(directory / "user.md").read_text(encoding="utf-8").rstrip("\n"),
            examples=shots,
        )


def _example(document: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    return str(document["ocr_text"]), document["output"]
