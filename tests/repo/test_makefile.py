"""The Makefile is a thin wrapper over docker compose (S0-AC-01/02/03/23/34/35/37/39)."""

import re

import pytest

from tests.repo.conftest import read_text
from tests.repo.makefile_parser import Makefile

CONTAINER_PREFIXES = ("$(DC)", "$(RUN)", "docker ")
HOST_TOOLS = re.compile(r"(?:^|[\s;&|(])(uv|python3?|pip3?|node|npm|npx|pytest|ruff|mypy)(?=\s|$)")

REQUIRED_TARGETS = [
    "build",
    "up",
    "down",
    "logs",
    "check",
    "lint",
    "format",
    "typecheck",
    "test",
    "fe-test",
    "fe-build",
    "api-client",
    "api-check",
    "migrate",
    "revision",
    "smoke",
    "hooks",
    "secrets",
]

CHECK_STEPS = [
    "lint",
    "typecheck",
    "test",
    "fe-lint",
    "fe-typecheck",
    "fe-test",
    "fe-build",
    "api-check",
    "secrets",
]


def _command_rules(makefile: Makefile) -> list[str]:
    return [t for t, r in makefile.rules.items() if r.recipe and not t.startswith(".")]


def _recipe_text(makefile: Makefile, target: str) -> str:
    return "\n".join(makefile.rule(target).commands)


def test_s0_ac_02_compose_wrappers_are_defined(makefile: Makefile) -> None:
    """S0-AC-02: DC wraps both compose files (plus a local compose.override.yaml when present,
    and the cache overlay on GitHub Actions) and RUN is a one-off `run --rm`."""
    assert makefile.variables["DC"] == (
        "docker compose -f compose.yaml -f compose.dev.yaml"
        "$(if $(wildcard compose.override.yaml), -f compose.override.yaml)"
        "$(if $(GITHUB_ACTIONS), -f compose.ci.yaml)"
    )
    assert makefile.variables["RUN"] == "$(DC) run --rm"


def test_s0_ac_02_every_recipe_line_runs_in_docker(makefile: Makefile) -> None:
    """S0-AC-02: recipes start with $(DC), $(RUN) or docker (help/hooks excepted)."""
    allowed_extra = {"help": ("awk ", "echo "), "hooks": ("git config ",)}
    offenders = [
        f"{target}: {command}"
        for target in _command_rules(makefile)
        for command in makefile.rule(target).commands
        if not command.startswith(CONTAINER_PREFIXES + allowed_extra.get(target, ()))
    ]

    assert offenders == []


def test_s0_ac_02_no_host_toolchain_invocations(makefile: Makefile) -> None:
    """S0-AC-02: uv/python/pip/node/npm/npx never run on the host, only after $(RUN)/$(DC)."""
    offenders = [
        f"{target}: {command}"
        for target in _command_rules(makefile)
        for command in makefile.rule(target).commands
        if not command.startswith(CONTAINER_PREFIXES) and HOST_TOOLS.search(command)
    ]

    assert offenders == []


def test_s0_ac_02_shell_operators_do_not_smuggle_host_commands(makefile: Makefile) -> None:
    """S0-AC-02: a docker line must not chain a host command with ;, && or ||."""
    offenders = [
        f"{target}: {command}"
        for target in _command_rules(makefile)
        for command in makefile.rule(target).commands
        if command.startswith(CONTAINER_PREFIXES)
        and re.search(r"(?:;|&&|\|\|)\s*(uv|python3?|pip3?|node|npm|npx)\b", command)
    ]

    assert offenders == []


def test_s0_ac_03_help_is_the_default_goal(makefile: Makefile) -> None:
    """S0-AC-03: `make` with no target prints the help list."""
    assert makefile.default_goal == "help"


@pytest.mark.parametrize("target", REQUIRED_TARGETS)
def test_s0_ac_03_required_targets_have_help_text(makefile: Makefile, target: str) -> None:
    """S0-AC-03: each required target exists and carries a `## description` for help."""
    assert makefile.rule(target).help


def test_s0_ac_03_help_lists_targets_from_help_comments(makefile: Makefile) -> None:
    """S0-AC-03: help is generated from `## ` comments, so the list cannot go stale."""
    recipe = _recipe_text(makefile, "help")

    assert "##" in recipe
    assert "MAKEFILE_LIST" in recipe


def test_s0_ac_02_all_command_targets_are_phony(makefile: Makefile) -> None:
    """S0-AC-02 / PLAN 2.5: every target is declared .PHONY (none produce files)."""
    phony = set(makefile.rule(".PHONY").prerequisites)
    missing = [t for t in makefile.rules if not t.startswith(".") and t not in phony]
    assert missing == []


def test_s0_ac_01_check_runs_every_gate_in_order(makefile: Makefile) -> None:
    """S0-AC-01: `make check` = lint typecheck test fe-lint fe-typecheck fe-test fe-build
    api-check secrets (stops on first failure)."""
    assert list(makefile.rule("check").prerequisites) == CHECK_STEPS


@pytest.mark.parametrize(
    ("target", "needles"),
    [
        ("lint", ["ruff check", "ruff format --check", "lint-imports"]),
        ("typecheck", ["mypy"]),
        ("test", ["pytest", "--cov"]),
        ("fe-lint", ["eslint"]),
        ("fe-typecheck", ["tsc"]),
        ("fe-test", ["vitest run", "--coverage"]),
        ("fe-build", ["build"]),
        ("secrets", ["gitleaks"]),
        ("format", ["ruff check --fix", "ruff format"]),
        ("lock", ["uv lock"]),
    ],
)
def test_s0_ac_01_gate_targets_run_the_documented_tools(
    makefile: Makefile, target: str, needles: list[str]
) -> None:
    """S0-AC-01: each gate target runs the tool PLAN 2.5 assigns to it, inside a container."""
    recipe = _recipe_text(makefile, target)
    for needle in needles:
        assert needle in recipe, f"`make {target}` does not run `{needle}`"


def test_s0_ac_01_prettier_check_is_part_of_the_frontend_gate(makefile: Makefile) -> None:
    """S0-AC-01: the Prettier check runs in the gate (fe-lint or its own step in check)."""
    gate = "\n".join(_recipe_text(makefile, t) for t in CHECK_STEPS)

    assert re.search(r"prettier\b.*--check|format:check", gate)


def test_s0_ac_01_python_tools_run_without_syncing_the_venv(makefile: Makefile) -> None:
    """S0-AC-01 / PLAN 2.3: tools run as `uv run --frozen --no-sync` (no writes to /opt/venv)."""
    for target in ("lint", "typecheck", "test"):
        for command in makefile.rule(target).commands:
            if "uv run" in command:
                assert "--frozen" in command
                assert "--no-sync" in command


def test_s0_ac_23_revision_uses_alembic_in_the_dev_container(makefile: Makefile) -> None:
    """S0-AC-23: `make revision m=...` runs `alembic revision -m` (no autogenerate)."""
    recipe = _recipe_text(makefile, "revision")

    assert "alembic revision" in recipe
    assert "-m" in recipe
    assert "--autogenerate" not in recipe


def test_s0_ac_23_migrate_runs_the_migrate_service(makefile: Makefile) -> None:
    """S0-AC-23: `make migrate` runs the compose `migrate` service."""
    assert re.search(
        r"\$\(DC\) run --rm migrate|\$\(RUN\) migrate", _recipe_text(makefile, "migrate")
    )


def test_s0_ac_34_ci_builds_checks_smokes_and_enforces_image_budget(makefile: Makefile) -> None:
    """S0-AC-34: `make ci` = build, check, smoke, plus the runtime image size budget."""
    rule = makefile.rule("ci")
    steps = list(rule.prerequisites)

    for step in ("build", "check", "smoke", "container-checks"):
        assert step in steps, f"`make ci` does not run `{step}`"
    assert steps.index("build") < steps.index("check") < steps.index("smoke")


def test_s0_ac_34_container_checks_run_the_checks_script(makefile: Makefile) -> None:
    """S0-AC-08/09/12/34: the container checks script runs inside a container, not on the host."""
    recipe = _recipe_text(makefile, "container-checks")

    assert "tests/container/run.sh" in recipe
    assert recipe.startswith(("$(DC)", "$(RUN)", "docker "))


def test_s0_ac_11_smoke_waits_for_the_stack_then_runs_the_smoke_service(
    makefile: Makefile,
) -> None:
    """S0-AC-11: `make smoke` starts the stack with --wait, then runs the curl smoke service."""
    commands = makefile.rule("smoke").commands

    assert any("up" in c and "--wait" in c for c in commands)
    assert any(re.search(r"run --rm smoke|\$\(RUN\) smoke", c) for c in commands)


def test_s0_ac_35_hooks_points_git_at_githooks(makefile: Makefile) -> None:
    """S0-AC-35: `make hooks` sets core.hooksPath=.githooks."""
    assert "git config core.hooksPath .githooks" in _recipe_text(makefile, "hooks")


def test_s0_ac_35_pre_commit_target_runs_pre_commit_in_dev(makefile: Makefile) -> None:
    """S0-AC-35: `make pre-commit` runs `pre-commit run --all-files` in the dev container."""
    assert "pre-commit run --all-files" in _recipe_text(makefile, "pre-commit")


def test_s0_ac_35_pre_commit_target_scans_staged_changes_for_secrets(makefile: Makefile) -> None:
    """S0-AC-35: `make pre-commit` also runs gitleaks on what is about to be committed."""
    assert "$(RUN) gitleaks staged" in _recipe_text(makefile, "pre-commit")


def test_s0_ac_04_secrets_self_tests_the_scan_before_scanning(makefile: Makefile) -> None:
    """S0-AC-04: `make secrets` first proves the scan catches a planted secret, then scans
    history and the working tree."""
    commands = [c.strip() for c in makefile.rule("secrets").recipe]

    assert commands == [
        "$(RUN) --entrypoint /bin/sh gitleaks /repo/tests/secrets/selftest.sh",
        "$(RUN) gitleaks all",
    ]


def test_s0_ac_37_outdated_reports_without_modifying(makefile: Makefile) -> None:
    """S0-AC-37: `make outdated` reports uv, npm and base-image updates and changes nothing."""
    recipe = _recipe_text(makefile, "outdated")

    assert "uv tree --outdated" in recipe
    assert "npm outdated" in recipe
    for mutating in ("uv lock", "uv sync", "npm install", "npm update", "npm ci", "--upgrade"):
        assert mutating not in recipe


def test_s0_ac_39_llm_config_validates_the_llm_compose_file(makefile: Makefile) -> None:
    """S0-AC-39: `make llm-config` runs `docker compose -f deploy/llm/compose.yaml config`."""
    recipe = _recipe_text(makefile, "llm-config")

    assert "docker compose -f deploy/llm/compose.yaml" in recipe
    assert " config" in recipe
    for target in ("llm-up", "llm-down"):
        assert "deploy/llm/compose.yaml" in _recipe_text(makefile, target)


@pytest.mark.parametrize("target", ["llm-up", "llm-down"])
def test_s0_ac_39_llm_targets_match_the_windows_readme_commands(
    makefile: Makefile, target: str
) -> None:
    """S0-AC-39: Windows runs the README's `docker compose` lines; Make runs the very same."""
    (command,) = makefile.rule(target).commands

    assert command.startswith("docker compose -f deploy/llm/compose.yaml --env-file ")
    assert command in read_text("README.md")


@pytest.mark.parametrize("target", ["down", "clean"])
def test_s0_ac_02_lifecycle_targets_never_delete_the_data_volume(
    makefile: Makefile, target: str
) -> None:
    """PLAN 2.5: `down` and `clean` never remove the data volume (`down -v`)."""
    recipe = _recipe_text(makefile, target)

    assert not re.search(r"down\b[^\n]*\s(-v|--volumes)\b", recipe)
    assert "volume rm" not in recipe


def test_s0_ac_32_api_client_generates_spec_and_types(makefile: Makefile) -> None:
    """S0-AC-32: `make api-client` exports openapi.json (dev) and runs openapi-typescript."""
    recipe = _recipe_text(makefile, "api-client")

    assert "paper-trail openapi" in recipe or "paper_trail openapi" in recipe
    assert "frontend/openapi.json" in recipe or "openapi.json" in recipe
    assert "openapi-typescript" in recipe
    assert "src/api/schema.d.ts" in recipe


def test_s0_ac_32_api_check_diffs_against_committed_files(makefile: Makefile) -> None:
    """S0-AC-32: `make api-check` regenerates into a temp dir and fails on any diff."""
    recipe = _recipe_text(makefile, "api-check")

    assert "openapi-typescript" in recipe
    assert "diff" in recipe


@pytest.mark.parametrize("target", ["api-check", "api-client"])
def test_s0_ac_32_api_targets_build_their_images_before_any_pipe(
    makefile: Makefile, target: str
) -> None:
    """On a fresh CI runner `compose run` built the images itself, and the build log went into
    the piped OpenAPI document (first public CI run): the images are built in a step of their
    own, never inside a pipe."""
    commands = makefile.rule(target).commands
    builds = [
        index
        for index, command in enumerate(commands)
        if re.search(r"\$\(DC\) build\b", command) and "dev" in command and "frontend" in command
    ]
    runs = [index for index, command in enumerate(commands) if "$(RUN)" in command]

    assert builds, commands
    assert all("|" not in commands[index] for index in builds)
    assert builds[0] < min(runs)
