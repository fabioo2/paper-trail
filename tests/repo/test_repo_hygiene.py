"""Repository hygiene: ignores, env docs, pyproject, injection, egress, docs.

Covers S0-AC-04, S0-AC-05, S0-AC-14, S0-AC-36, S0-AC-38.
"""

import ast
import json
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from paper_trail.config import Settings
from tests.repo.conftest import read_text
from tests.support import REPO_ROOT

SRC = REPO_ROOT / "src" / "paper_trail"
CLI_MAIN = SRC / "interfaces" / "cli" / "main.py"
COMPOSE_ONLY_VARIABLES = (
    "PAPER_TRAIL_BIND_ADDR",
    "PAPER_TRAIL_PORT",
    "PAPER_TRAIL_DATA_VOLUME",
    "PAPER_TRAIL_UID",
    "PAPER_TRAIL_GID",
    "PAPER_TRAIL_RECEIPTS_DIR",  # agent core: the read-only receipts folder of the cli service
)
TELEMETRY_PACKAGES = re.compile(
    r"^(sentry|opentelemetry|ddtrace|datadog|newrelic|posthog|rollbar|bugsnag|honeybadger"
    r"|elastic-apm|logfire|segment|analytics-python|mixpanel|amplitude|scout-apm|raygun)",
    re.IGNORECASE,
)
FRONTEND_TELEMETRY = re.compile(
    r"^(@sentry/|posthog|@vercel/analytics|@vercel/speed-insights|mixpanel|@datadog/|react-ga"
    r"|logrocket|@amplitude/|@segment/|@bugsnag/|rollbar|@newrelic/|web-vitals|plausible)",
    re.IGNORECASE,
)
STAGE0_RUNTIME_DEPENDENCIES = (
    "fastapi",
    "uvicorn",
    "pydantic",
    "pydantic-settings",
    "sqlalchemy",
    "alembic",
    "sqlite-vec",
    "structlog",
    "typer",
)
DEV_DEPENDENCIES = {
    "pytest",
    "pytest-cov",
    "pytest-xdist",
    "httpx",
    "hypothesis",
    "mypy",
    "ruff",
    "import-linter",
    "pre-commit",
    "pyyaml",
    "types-pyyaml",
}
HTTP_CLIENT_MODULES = {"httpx", "requests", "aiohttp", "urllib3", "urllib.request", "http.client"}
OLLAMA_ADAPTER = SRC / "adapters" / "ollama"


def _python_files(root: Path) -> Iterator[Path]:
    yield from sorted(root.rglob("*.py"))


def _imported_modules(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names)


@pytest.fixture(scope="module")
def pyproject() -> dict[str, Any]:
    return tomllib.loads(read_text("pyproject.toml"))


def _requirement_name(requirement: str) -> str:
    return re.split(r"[\s\[<>=!~;]", requirement, maxsplit=1)[0]


# --- S0-AC-04 ---------------------------------------------------------------------------------


def _ignore_lines(relative: str) -> set[str]:
    return {
        line.strip()
        for line in read_text(relative).splitlines()
        if line.strip() and not line.startswith("#")
    }


@pytest.mark.parametrize(
    "alternatives",
    [
        (".env",),
        ("/data/",),  # anchored: an unanchored data/ would also hide tests/data/
        ("node_modules/", "**/node_modules/"),
        ("frontend/dist/", "dist/", "/frontend/dist/"),
        (".venv/",),
        (".coverage", ".coverage*"),
        ("coverage.xml",),
        ("htmlcov/",),
        ("frontend/coverage/", "coverage/"),
        ("/backups/",),
        ("/receipts/",),  # anchored: receipts/ would also hide frontend/src/features/receipts/
        ("__pycache__/",),
        ("compose.override.yaml", "/compose.override.yaml"),
    ],
)
def test_s0_ac_04_gitignore_covers_local_data_and_build_output(
    alternatives: tuple[str, ...],
) -> None:
    """S0-AC-04: .env, data/, node_modules/, frontend/dist/, .venv/, coverage and the per-host
    compose.override.yaml are ignored."""
    assert _ignore_lines(".gitignore") & set(alternatives), alternatives


def test_s0_ac_04_env_example_is_not_ignored() -> None:
    """S0-AC-04: .env is ignored but .env.example must stay committed."""
    lines = _ignore_lines(".gitignore")
    broad = {".env*", ".env.*", "*.env"}

    assert not lines & broad or "!.env.example" in lines


@pytest.mark.parametrize(
    "alternatives",
    [
        (".git",),
        (".env",),
        ("data", "data/", "/data"),
        ("backups", "backups/", "/backups"),
        ("receipts", "receipts/", "/receipts"),
        ("node_modules", "**/node_modules"),
    ],
)
def test_s0_ac_04_dockerignore_excludes_secrets_and_bulk(alternatives: tuple[str, ...]) -> None:
    """S0-AC-04: .dockerignore excludes .git, .env, local data (data/, backups/, receipts/) and
    node_modules."""
    assert _ignore_lines(".dockerignore") & set(alternatives), alternatives


def _settings_env_names() -> list[str]:
    names: list[str] = []
    for name, field in Settings.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            names.extend(
                f"PAPER_TRAIL_{name}__{nested}".upper() for nested in annotation.model_fields
            )
        else:
            names.append(f"PAPER_TRAIL_{name}".upper())
    return names


def test_s0_ac_04_env_example_documents_every_setting_with_a_comment() -> None:
    """S0-AC-04: .env.example lists every PAPER_TRAIL_* setting of this stage, each commented."""
    lines = read_text(".env.example").splitlines()
    expected = [*_settings_env_names(), *COMPOSE_ONLY_VARIABLES]

    for variable in expected:
        pattern = re.compile(rf"^#?\s*{variable}=")
        indexes = [i for i, line in enumerate(lines) if pattern.match(line)]
        assert len(indexes) == 1, f"{variable} must appear exactly once in .env.example"
        previous = next((lines[i] for i in range(indexes[0] - 1, -1, -1) if lines[i].strip()), "")
        assert previous.startswith("#"), f"{variable} needs a comment line above it"


def test_s0_ac_04_env_example_mentions_no_unknown_settings() -> None:
    """S0-AC-04: .env.example does not document settings that do not exist (no drift)."""
    documented = set(
        re.findall(r"^#?\s*(PAPER_TRAIL_[A-Z0-9_]+)=", read_text(".env.example"), re.M)
    )

    assert documented <= {*_settings_env_names(), *COMPOSE_ONLY_VARIABLES}


def test_s0_ac_04_env_example_holds_no_secrets() -> None:
    """S0-AC-04: no secrets exist in this design; .env.example contains only safe defaults."""
    text = read_text(".env.example")

    assert not re.search(r"(?i)(password|secret|token|api_key)\s*=\s*\S", text)


# --- S0-AC-05 ---------------------------------------------------------------------------------


def test_s0_ac_05_python_version_and_console_script(pyproject: dict[str, Any]) -> None:
    """S0-AC-05: requires-python >=3.12,<3.13 and the paper-trail console script."""
    project = pyproject["project"]

    assert project["name"] == "paper-trail"
    assert project["requires-python"].replace(" ", "") == ">=3.12,<3.13"
    assert project["scripts"] == {"paper-trail": "paper_trail.interfaces.cli.main:app"}
    assert pyproject["build-system"]["build-backend"] == "hatchling.build"


def test_s0_ac_05_stage0_runtime_dependencies(pyproject: dict[str, Any]) -> None:
    """S0-AC-05 / PLAN 2.2: the Stage 0 runtime dependencies, with lower bounds."""
    deps = {_requirement_name(d).lower(): d for d in pyproject["project"]["dependencies"]}

    for name in STAGE0_RUNTIME_DEPENDENCIES:
        assert name in deps, name
        assert ">=" in deps[name], f"{name} needs a lower bound"


def test_s0_ac_05_dev_dependency_group(pyproject: dict[str, Any]) -> None:
    """S0-AC-05 / PLAN 2.2: the dev group carries the test, lint and type tooling."""
    dev = {_requirement_name(d).lower() for d in pyproject["dependency-groups"]["dev"]}

    assert dev >= DEV_DEPENDENCIES


def test_s0_ac_05_ruff_config(pyproject: dict[str, Any]) -> None:
    """S0-AC-05: ruff line length 100, py312, the documented rule set and test ignores."""
    ruff = pyproject["tool"]["ruff"]

    assert ruff["line-length"] == 100
    assert ruff["target-version"] == "py312"
    selected = set(ruff["lint"]["select"])
    assert selected >= {
        "E",
        "W",
        "F",
        "I",
        "B",
        "UP",
        "SIM",
        "N",
        "S",
        "C4",
        "RET",
        "PTH",
        "PL",
        "RUF",
        "ANN",
        "DTZ",
        "T20",
        "ERA",
    }
    per_file = ruff["lint"]["per-file-ignores"]
    test_ignores = next(v for k, v in per_file.items() if k.startswith("tests"))
    assert set(test_ignores) >= {"S101", "PLR2004", "ANN"}


def test_s0_ac_05_mypy_is_strict(pyproject: dict[str, Any]) -> None:
    """S0-AC-05: mypy strict with the pydantic plugin over src, tests and migrations."""
    mypy = pyproject["tool"]["mypy"]

    assert mypy["strict"] is True
    assert "pydantic.mypy" in mypy["plugins"]
    assert set(mypy["files"]) >= {"src", "tests", "migrations"}


def test_s0_ac_05_pytest_and_coverage_config(pyproject: dict[str, Any]) -> None:
    """S0-AC-05 / PLAN 1.7: strict pytest options and branch coverage with fail_under 90."""
    pytest_options = pyproject["tool"]["pytest"]["ini_options"]
    addopts = pytest_options["addopts"]
    addopts = " ".join(addopts) if isinstance(addopts, list) else addopts

    for option in ("--strict-markers", "--strict-config", "-ra"):
        assert option in addopts
    assert pytest_options["xfail_strict"] is True
    assert "error" in pytest_options["filterwarnings"]
    coverage = pyproject["tool"]["coverage"]
    assert coverage["run"]["branch"] is True
    assert coverage["report"]["fail_under"] == 90


def test_s0_ac_05_import_linter_is_configured(pyproject: dict[str, Any]) -> None:
    """S0-AC-05 / S0-AC-06: import-linter is configured for the paper_trail package."""
    importlinter = pyproject["tool"]["importlinter"]

    assert importlinter["root_package"] == "paper_trail"
    assert len(importlinter["contracts"]) >= 1


def test_s0_ac_05_lockfile_is_committed() -> None:
    """S0-AC-05: uv.lock is present (and `uv sync --frozen` is used in the build)."""
    assert "paper-trail" in read_text("uv.lock")


# --- S0-AC-14 ---------------------------------------------------------------------------------


def _environment_reads(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "environb"}) or (
            isinstance(node, ast.ImportFrom)
            and node.module == "os"
            and {a.name for a in node.names} & {"environ", "getenv", "environb"}
        ):
            lines.append(node.lineno)
    return lines


def _settings_instantiations(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "Settings")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "Settings")
        )
    ]


def test_s0_ac_14_only_the_cli_reads_the_environment() -> None:
    """S0-AC-14: no module in src/ reads os.environ/os.getenv except interfaces/cli/main.py."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{line}"
        for path in _python_files(SRC)
        if path != CLI_MAIN
        for line in _environment_reads(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert offenders == []


def test_s0_ac_14_only_the_cli_instantiates_settings() -> None:
    """S0-AC-14: Settings() is constructed only in interfaces/cli/main.py (no singleton)."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{line}"
        for path in [*_python_files(SRC), *_python_files(REPO_ROOT / "migrations")]
        if path != CLI_MAIN
        for line in _settings_instantiations(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert offenders == []
    assert _settings_instantiations(ast.parse(CLI_MAIN.read_text(encoding="utf-8")))


def test_s0_ac_14_no_cached_settings_accessor() -> None:
    """S0-AC-14: there is no module-level settings singleton or get_settings() cache."""
    for path in _python_files(SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                assert node.name != "get_settings", path


def test_s0_ac_14_migrations_env_does_not_read_the_environment() -> None:
    """S0-AC-14 / S0-AC-23: migrations/env.py takes its URL from the Alembic config it is given."""
    env_py = REPO_ROOT / "migrations" / "env.py"

    assert _environment_reads(ast.parse(env_py.read_text(encoding="utf-8"))) == []


def test_s0_ac_23_alembic_ini_has_no_database_url() -> None:
    """S0-AC-23: alembic.ini sets script_location and never a sqlalchemy.url."""
    text = read_text("alembic.ini")

    assert re.search(r"^script_location\s*=\s*migrations\s*$", text, re.MULTILINE)
    assert not re.search(r"^sqlalchemy\.url\s*=\s*\S", text, re.MULTILINE)


def test_s0_ac_23_baseline_migration_is_revision_0001() -> None:
    """S0-AC-23: migrations/versions/0001_baseline.py is the root revision "0001"."""
    tree = ast.parse(read_text("migrations/versions/0001_baseline.py"))
    assignments = {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name) and node.value is not None
    }

    assert ast.literal_eval(assignments["revision"]) == "0001"
    assert ast.literal_eval(assignments["down_revision"]) is None


# --- S0-AC-38 ---------------------------------------------------------------------------------


def test_s0_ac_38_no_backend_telemetry_dependencies(pyproject: dict[str, Any]) -> None:
    """S0-AC-38: no telemetry or error-reporting SDKs among Python dependencies."""
    names = [_requirement_name(d) for d in pyproject["project"]["dependencies"]]
    names += [_requirement_name(d) for g in pyproject["dependency-groups"].values() for d in g]

    assert [n for n in names if TELEMETRY_PACKAGES.match(n)] == []


def test_s0_ac_38_no_telemetry_packages_in_the_lockfile() -> None:
    """S0-AC-38: no telemetry SDK arrives transitively either."""
    locked = re.findall(r'^name = "([^"]+)"', read_text("uv.lock"), re.MULTILINE)

    # FastAPI requires the no-op OpenTelemetry API; without an SDK or exporter it records and
    # sends nothing (and the app disables FastAPI's telemetry hooks explicitly).
    offenders = [n for n in locked if TELEMETRY_PACKAGES.match(n) and n != "opentelemetry-api"]
    assert offenders == []


def test_s0_ac_38_no_frontend_telemetry_dependencies() -> None:
    """S0-AC-38: no analytics or error-reporting packages in the SPA."""
    package = json.loads(read_text("frontend/package.json"))
    names = [*package.get("dependencies", {}), *package.get("devDependencies", {})]

    assert [n for n in names if FRONTEND_TELEMETRY.match(n)] == []


def test_s0_ac_38_only_the_ollama_adapter_makes_http_calls() -> None:
    """S0-AC-38: HTTP client libraries are imported only under adapters/ollama/."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {module}"
        for path in _python_files(SRC)
        if OLLAMA_ADAPTER not in path.parents
        for module in _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        if module in HTTP_CLIENT_MODULES or module.split(".")[0] in {"httpx", "requests", "aiohttp"}
    ]

    assert offenders == []


def test_s0_ac_38_index_html_loads_nothing_external() -> None:
    """S0-AC-38: fonts, icons and scripts are self-hosted (no CDN links in index.html)."""
    html = read_text("frontend/index.html")

    assert not re.search(r"""(?:src|href)\s*=\s*["']?(?:https?:)?//""", html, re.IGNORECASE)


# --- S0-AC-36 ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def readme() -> str:
    return read_text("README.md")


def test_s0_ac_39_windows_llm_host_steps_need_no_make(readme: str) -> None:
    """S0-AC-39: the Makefile needs bash, so the Windows steps use plain `docker compose`."""
    windows = readme.split("### Windows 11", 1)[1].split("### Linux", 1)[0]
    commands = re.findall(r"```powershell\n(.*?)```", windows, re.DOTALL)

    assert commands, "the Windows section has PowerShell blocks"
    assert not any(re.search(r"^\s*make\b", block, re.MULTILINE) for block in commands)


DOCS_WITH_LINKS = ("README.md", "CONTRIBUTING.md")
MARKDOWN_LINK = re.compile(r"\]\(([^)\s]+)\)")


@pytest.mark.parametrize("document", DOCS_WITH_LINKS)
def test_s0_ac_36_relative_links_resolve(document: str) -> None:
    """S0-AC-36: every repository path the README and CONTRIBUTING link to (the ADRs, the
    plans, the deployment overlay, the config template) exists."""
    targets = [
        link.split("#", 1)[0]
        for link in MARKDOWN_LINK.findall(read_text(document))
        if not re.match(r"[a-z][a-z0-9+.-]*:|#", link)
    ]

    assert targets, f"{document} links to repository files"
    missing = [target for target in targets if not (REPO_ROOT / target).exists()]
    assert missing == [], f"{document} links to missing paths"
