"""GitHub Actions CI and Dependabot (S0-AC-34, S0-AC-37)."""

import re
from typing import Any

import pytest

from tests.repo.conftest import load_yaml

JOBS = {"backend", "frontend", "contract", "secrets", "image"}
FORBIDDEN_RUN = re.compile(r"\b(pip install|npm (ci|install)|uv (sync|pip)|npx)\b")
FORBIDDEN_ACTIONS = ("actions/setup-python", "actions/setup-node", "astral-sh/setup-uv")


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    data: dict[str, Any] = load_yaml(".github/workflows/ci.yml")
    return data


def _steps(workflow: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(name, step) for name, job in workflow["jobs"].items() for step in job["steps"]]


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    triggers: dict[str, Any] = workflow.get("on", workflow.get(True))  # type: ignore[assignment,call-overload]
    return triggers


def test_s0_ac_34_workflow_triggers(workflow: dict[str, Any]) -> None:
    """S0-AC-34: runs on push to main and on pull requests."""
    triggers = _triggers(workflow)

    assert "main" in triggers["push"]["branches"]
    assert "pull_request" in triggers


def test_s0_ac_34_least_privilege_and_concurrency(workflow: dict[str, Any]) -> None:
    """S0-AC-34: permissions contents: read; superseded runs are cancelled."""
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is True
    for name, job in workflow["jobs"].items():
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}, name


def test_s0_ac_34_defines_exactly_the_documented_jobs(workflow: dict[str, Any]) -> None:
    """S0-AC-34: jobs backend, frontend, contract, secrets, image; no deploy job."""
    assert set(workflow["jobs"]) == JOBS


def test_s0_ac_34_job_dependencies(workflow: dict[str, Any]) -> None:
    """S0-AC-34 / PLAN 2.6: image needs backend+frontend; contract needs both dev images."""
    jobs = workflow["jobs"]

    assert set(jobs["image"]["needs"]) >= {"backend", "frontend"}
    assert "needs" in jobs["contract"]


def test_s0_ac_34_every_run_step_calls_make(workflow: dict[str, Any]) -> None:
    """S0-AC-34: every run step calls a make target, so CI and local runs are identical."""
    for job, step in _steps(workflow):
        if "run" not in step:
            continue
        for line in filter(None, (raw.strip() for raw in step["run"].splitlines())):
            assert line.startswith("make "), f"{job}: `{line}` is not a make target"
            assert not FORBIDDEN_RUN.search(line), f"{job}: {line}"


def test_s0_ac_34_no_host_toolchain_setup_actions(workflow: dict[str, Any]) -> None:
    """S0-AC-34: no setup-python / setup-node; tooling comes from the dev images."""
    for job, step in _steps(workflow):
        uses = step.get("uses", "")
        assert not uses.startswith(FORBIDDEN_ACTIONS), f"{job}: {uses}"


def test_s0_ac_34_actions_are_version_pinned(workflow: dict[str, Any]) -> None:
    """S0-AC-34 / S0-AC-37: every action is pinned to a tag or SHA (Dependabot bumps them)."""
    for job, step in _steps(workflow):
        if "uses" in step:
            assert re.search(r"@(v\d+[\w.]*|[0-9a-f]{40})$", step["uses"]), f"{job}: {step['uses']}"


def test_s0_ac_34_buildx_with_gha_cache(workflow: dict[str, Any]) -> None:
    """S0-AC-34 / PLAN 2.6: every job that builds images sets up buildx and exposes the GHA
    cache credentials, so compose.ci.yaml's type=gha cache works."""
    building = {"backend", "frontend", "contract", "image"}
    for job in building:
        uses = [step.get("uses", "") for step in workflow["jobs"][job]["steps"]]
        assert any(u.startswith("docker/setup-buildx-action@") for u in uses), job
        assert any(u.startswith("crazy-max/ghaction-github-runtime@") for u in uses), job


def test_s0_ac_34_ci_overlay_caches_every_built_image_in_gha() -> None:
    """PLAN 2.6: compose.ci.yaml gives each built service its own type=gha cache scope."""
    overlay = load_yaml("compose.ci.yaml")["services"]
    built = {"migrate", "dev", "frontend"}

    assert set(overlay) == built
    scopes = set()
    for name in built:
        build = overlay[name]["build"]
        (cache_from,) = build["cache_from"]
        (cache_to,) = build["cache_to"]
        scope = re.fullmatch(r"type=gha,scope=([\w-]+)", cache_from)
        assert scope, cache_from
        assert cache_to == f"{cache_from},mode=max"
        scopes.add(scope.group(1))
    assert len(scopes) == len(built), "each image needs its own cache scope"


def test_s0_ac_34_secrets_scan_sees_full_history(workflow: dict[str, Any]) -> None:
    """S0-AC-34: the secrets job checks out with fetch-depth: 0 and runs make secrets."""
    steps = workflow["jobs"]["secrets"]["steps"]
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))

    assert checkout["with"]["fetch-depth"] == 0
    assert any("make secrets" in s.get("run", "") for s in steps)


@pytest.mark.parametrize(
    ("job", "targets"),
    [
        ("backend", ["lint", "typecheck", "test"]),
        ("frontend", ["fe-install", "fe-lint", "fe-typecheck", "fe-test", "fe-build"]),
        ("contract", ["api-check"]),
        ("secrets", ["secrets"]),
        ("image", ["smoke", "container-checks", "down"]),
    ],
)
def test_s0_ac_34_jobs_run_their_make_targets(
    workflow: dict[str, Any], job: str, targets: list[str]
) -> None:
    """S0-AC-34 / PLAN 2.6: each job runs the documented make targets."""
    invoked = " ".join(s.get("run", "") for s in workflow["jobs"][job]["steps"]).split()
    for target in targets:
        assert target in invoked, f"{job} does not run make {target}"


def test_s0_ac_34_image_job_binds_loopback(workflow: dict[str, Any]) -> None:
    """S0-AC-34: the smoke test in CI binds 127.0.0.1."""
    job = workflow["jobs"]["image"]
    env = {**workflow.get("env", {}), **job.get("env", {})}
    for step in job["steps"]:
        env.update(step.get("env", {}))

    assert env.get("PAPER_TRAIL_BIND_ADDR") == "127.0.0.1"


def test_s0_ac_34_backend_uploads_coverage(workflow: dict[str, Any]) -> None:
    """S0-AC-34 / PLAN 2.6: coverage.xml is uploaded as an artifact."""
    steps = workflow["jobs"]["backend"]["steps"]
    upload = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@")]

    assert upload
    assert "coverage.xml" in str(upload[0]["with"]["path"])


# --- Dependabot (S0-AC-37) --------------------------------------------------------------------


@pytest.fixture(scope="module")
def dependabot() -> dict[str, Any]:
    data: dict[str, Any] = load_yaml(".github/dependabot.yml")
    return data


def test_s0_ac_37_dependabot_version_2(dependabot: dict[str, Any]) -> None:
    """S0-AC-37: .github/dependabot.yml is version 2."""
    assert dependabot["version"] == 2


@pytest.mark.parametrize(
    ("ecosystem", "directory"),
    [("docker", "/"), ("uv", "/"), ("npm", "/frontend"), ("github-actions", "/")],
)
def test_s0_ac_37_weekly_grouped_updates(
    dependabot: dict[str, Any], ecosystem: str, directory: str
) -> None:
    """S0-AC-37: weekly, grouped updates for docker, uv, npm (/frontend) and github-actions."""
    matches = [
        update
        for update in dependabot["updates"]
        if update["package-ecosystem"] == ecosystem
        and directory in (update.get("directory"), *update.get("directories", []))
    ]

    assert matches, f"no {ecosystem} update for {directory}"
    (update,) = matches
    assert update["schedule"]["interval"] == "weekly"
    assert update["groups"], "updates must be grouped"
