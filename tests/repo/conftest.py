"""Fixtures for static repository checks."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.repo.makefile_parser import Makefile, parse_makefile
from tests.support import REPO_ROOT


def load_yaml(relative: str) -> Any:
    path = REPO_ROOT / relative
    assert path.is_file(), f"missing {relative}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def read_text(relative: str) -> str:
    path = REPO_ROOT / relative
    assert path.is_file(), f"missing {relative}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def makefile() -> Makefile:
    return parse_makefile(REPO_ROOT / "Makefile")


@pytest.fixture(scope="session")
def compose() -> dict[str, Any]:
    data: dict[str, Any] = load_yaml("compose.yaml")
    return data


@pytest.fixture(scope="session")
def compose_dev() -> dict[str, Any]:
    data: dict[str, Any] = load_yaml("compose.dev.yaml")
    return data


@pytest.fixture(scope="session")
def compose_llm() -> dict[str, Any]:
    data: dict[str, Any] = load_yaml("deploy/llm/compose.yaml")
    return data
