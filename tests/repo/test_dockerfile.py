"""Multi-stage Dockerfile structure (S0-AC-08, S0-AC-37).

The built image itself is inspected by tests/container/run.sh (size, user, contents).
"""

import re

import pytest

from tests.repo.conftest import read_text

STAGES = [
    "uv",
    "frontend-dev",
    "frontend-build",
    "python-base",
    "python-deps",
    "python-dev",
    "runtime",
]


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return read_text("Dockerfile")


def _stage_body(dockerfile: str, stage: str) -> str:
    match = re.search(
        rf"^FROM\s+\S+\s+AS\s+{re.escape(stage)}\s*$(.*?)(?=^FROM\s|\Z)",
        dockerfile,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    assert match, f"Dockerfile has no `{stage}` stage"
    return match.group(1)


def _external_images(dockerfile: str) -> list[str]:
    """Every image referenced by FROM, COPY --from or RUN --mount=from that is not a stage."""
    refs = re.findall(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", dockerfile, re.MULTILINE)
    refs += re.findall(r"--from=([^\s,]+)", dockerfile)
    return [ref for ref in refs if ref not in STAGES]


def test_s0_ac_37_base_images_are_literal_pinned_from_lines(dockerfile: str) -> None:
    """S0-AC-37: python:3.12-slim-trixie, node:24-trixie-slim, uv x.y.z, literal on FROM.

    Dependabot's docker updater only reads literal image references, so no ARG indirection.
    """
    froms = re.findall(r"^FROM\s+(\S+)\s+AS\s+(\S+)", dockerfile, re.MULTILINE | re.IGNORECASE)
    by_stage = {stage: image for image, stage in froms}

    assert by_stage["python-base"] == "python:3.12-slim-trixie"
    assert by_stage["frontend-dev"] == "node:24-trixie-slim"
    assert re.fullmatch(r"ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+", by_stage["uv"]), by_stage["uv"]
    assert not re.search(r"^ARG\s+\w*_IMAGE\b", dockerfile, re.MULTILINE), "no ARG-based images"


def test_s0_ac_37_every_external_image_is_a_literal_versioned_reference(dockerfile: str) -> None:
    """S0-AC-37: every non-stage FROM / --from image is literal, tagged and not :latest."""
    images = _external_images(dockerfile)

    assert images, "expected at least one external base image"
    for image in images:
        assert "$" not in image, f"{image}: ARG-substituted image is invisible to Dependabot"
        name, sep, tag = image.partition("@")[0].rpartition(":")
        assert sep and "/" not in tag, f"{image}: untagged base image"
        assert tag != "latest", f"{image}: uses :latest"
        assert re.search(r"\d", tag), f"{image}: tag carries no version"
        assert name


def test_s0_ac_37_each_base_image_appears_once(dockerfile: str) -> None:
    """S0-AC-37: one FROM per base image; later stages reuse it by stage name (DRY)."""
    images = _external_images(dockerfile)

    assert len(images) == len(set(images)), images


def test_s0_ac_08_all_stages_exist_in_order(dockerfile: str) -> None:
    """S0-AC-08 / PLAN 2.3: the documented multi-stage layout, runtime last."""
    names = re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", dockerfile, re.MULTILINE | re.IGNORECASE)

    assert names == STAGES


def test_s0_ac_08_runtime_runs_as_non_root_app_user(dockerfile: str) -> None:
    """S0-AC-08: runtime creates `app` with a build-arg UID (default 1000) and switches to it."""
    runtime = _stage_body(dockerfile, "runtime")

    assert re.search(r"ARG\s+\w*UID=1000", dockerfile)
    assert re.search(r"useradd\b.*\bapp\b", runtime)
    assert re.search(r"^USER\s+app\b", runtime, re.MULTILINE)


def test_s0_ac_08_runtime_contains_spa_and_no_toolchains(dockerfile: str) -> None:
    """S0-AC-08: the SPA is copied to /app/static; uv is only bind-mounted, never copied."""
    runtime = _stage_body(dockerfile, "runtime")

    assert re.search(r"COPY\s+--from=frontend-build\s+/app/frontend/dist\s+/app/static", runtime)
    assert not re.search(r"COPY\s+--from=uv\b", runtime), "uv must not land in the runtime image"
    assert "--no-dev" in runtime
    assert "--frozen" in runtime


def test_s0_ac_08_runtime_entrypoint_healthcheck_and_labels(dockerfile: str) -> None:
    """S0-AC-08 / PLAN 2.3: ENTRYPOINT paper-trail, CMD serve, readiness HEALTHCHECK, OCI labels."""
    runtime = _stage_body(dockerfile, "runtime")

    assert re.search(r'^ENTRYPOINT \["paper-trail"\]', runtime, re.MULTILINE)
    assert re.search(r'^CMD \["serve"\]', runtime, re.MULTILINE)
    assert re.search(r"^HEALTHCHECK\b.*?/api/health/ready", runtime, re.MULTILINE | re.DOTALL)
    assert re.search(r"^EXPOSE 8000$", runtime, re.MULTILINE)
    for label in ("title", "version", "revision", "source"):
        assert f"org.opencontainers.image.{label}" in runtime, label
    assert "PAPER_TRAIL_BUILD_SHA" in runtime


def test_s0_ac_08_build_caches_and_no_secrets_in_args(dockerfile: str) -> None:
    """S0-AC-08 / PLAN 2.3: uv and npm use cache mounts; no secret-looking build args."""
    assert re.search(r"--mount=type=cache,[^\n]*uv", dockerfile)
    assert re.search(r"--mount=type=cache,[^\n]*npm", dockerfile)
    assert not re.search(r"^ARG\s+\w*(TOKEN|SECRET|PASSWORD|KEY)\w*", dockerfile, re.MULTILINE)


def test_s0_ac_05_dependencies_are_installed_from_the_lockfile(dockerfile: str) -> None:
    """S0-AC-05: every `uv sync` uses --frozen (uv.lock is authoritative)."""
    syncs = re.findall(r"uv sync[^\n]*", dockerfile)

    assert syncs
    assert all("--frozen" in sync for sync in syncs), syncs


def test_s0_ac_08_python_dev_installs_dependencies_before_copying_sources(
    dockerfile: str,
) -> None:
    """S0-AC-08: a source edit must not invalidate the python-dev dependency layer."""
    dev = _stage_body(dockerfile, "python-dev")
    deps = dev.find("--all-groups --no-install-project")
    copy_src = re.search(r"^COPY\s+src/", dev, re.MULTILINE)

    assert deps != -1, "python-dev must sync dependencies without the project first"
    assert copy_src, "python-dev must copy the sources for the editable install"
    assert deps < copy_src.start()


def test_s0_ac_08_runtime_build_metadata_comes_after_the_heavy_layers(dockerfile: str) -> None:
    """S0-AC-08: GIT_SHA/APP_VERSION and the labels follow the user, venv and SPA layers."""
    runtime = _stage_body(dockerfile, "runtime")
    metadata = min(
        runtime.index("ARG GIT_SHA"), runtime.index("ARG APP_VERSION"), runtime.index("LABEL ")
    )

    for layer in ("useradd", "COPY --from=python-deps", "uv sync", "COPY --from=frontend-build"):
        assert runtime.index(layer) < metadata, f"{layer} must precede the build metadata"
