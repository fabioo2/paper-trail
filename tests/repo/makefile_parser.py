"""A deliberately small GNU Make parser for static checks of the project Makefile.

It understands what the project Makefile is allowed to contain: variable assignments,
explicit rules with ``## help`` comments, tab-indented recipes and backslash continuations.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

_ASSIGNMENT = re.compile(r"^(?:export\s+)?(\.?[A-Za-z_][A-Za-z0-9_]*)\s*(?::=|\?=|\+=|=)\s*(.*)$")
_RULE = re.compile(r"^([A-Za-z0-9_.%/-]+(?:\s+[A-Za-z0-9_.%/-]+)*)\s*:(?!=)\s*(.*)$")
_RECIPE_MODIFIERS = "@-+"


@dataclass(frozen=True)
class Rule:
    target: str
    prerequisites: tuple[str, ...]
    help: str | None
    recipe: tuple[str, ...] = field(default=())

    @property
    def commands(self) -> tuple[str, ...]:
        """Recipe lines without the @, - and + modifiers."""
        return tuple(line.lstrip(_RECIPE_MODIFIERS).strip() for line in self.recipe)


@dataclass(frozen=True)
class Makefile:
    variables: dict[str, str]
    rules: dict[str, Rule]
    first_target: str | None

    def rule(self, target: str) -> Rule:
        assert target in self.rules, f"Makefile has no `{target}` target"
        return self.rules[target]

    @property
    def default_goal(self) -> str | None:
        return self.variables.get(".DEFAULT_GOAL", self.first_target)


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        if raw.endswith("\\"):
            buffer += raw[:-1] + " "
            continue
        lines.append(buffer + raw)
        buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


def parse_makefile(path: Path) -> Makefile:
    variables: dict[str, str] = {}
    rules: dict[str, Rule] = {}
    current: list[str] = []
    first_target: str | None = None

    for line in _logical_lines(path.read_text(encoding="utf-8")):
        if line.startswith("\t"):
            for target in current:
                rule = rules[target]
                rules[target] = Rule(
                    rule.target, rule.prerequisites, rule.help, (*rule.recipe, line.strip())
                )
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if rule_match := _RULE.match(stripped):
            body, _, help_text = rule_match.group(2).partition("##")
            prerequisites = tuple(body.split("#")[0].split())
            current = rule_match.group(1).split()
            for target in current:
                if target.startswith(".") and target in rules:  # .PHONY may repeat
                    prerequisites = (*rules[target].prerequisites, *prerequisites)
                rules[target] = Rule(target, prerequisites, help_text.strip() or None)
                if first_target is None and not target.startswith("."):
                    first_target = target
            continue
        if assignment := _ASSIGNMENT.match(stripped):
            variables[assignment.group(1)] = assignment.group(2).strip()
        current = []
    return Makefile(variables, rules, first_target)
