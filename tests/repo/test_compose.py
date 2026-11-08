"""Compose files: services, ports, hardening, pinning, LLM host (S0-AC-07/10/12/36/37/39).

These read the raw YAML (anchors and merge keys resolved by PyYAML). Interpolated values are
checked again against `docker compose config` by tests/container/run.sh.
"""

import re
from typing import Any

import pytest

from tests.repo.conftest import load_yaml, read_text

RUNTIME_SERVICES = {"migrate", "web", "worker"}
# One-off services of compose.yaml, started only on demand behind their own profile.
ON_DEMAND_SERVICES = {"cli", "mcp"}  # agent core: `make process` / `make pt`; `make mcp-up`
DEV_SERVICES = {"dev", "frontend", "vite", "gitleaks", "smoke", "container-checks"}
APP_IMAGE = "paper-trail:${PAPER_TRAIL_IMAGE_TAG:-latest}"
BIND = "${PAPER_TRAIL_BIND_ADDR:-127.0.0.1}"
HOST_PORTS_IN_USE = frozenset(
    (
        22,
        53,
        80,
        81,
        139,
        443,
        445,
        3001,
        5656,
        6767,
        7878,
        8080,
        8081,
        8096,
        8123,
        8989,
        8990,
        9191,
        9696,
        17287,
        18554,
        18555,
        61208,
        51820,
    )
)  # PLAN 1.1, checked 2026-11-06
PINNED_TAG = re.compile(r"^[^:@\s]+:v?[0-9][\w.-]*$")
INTERPOLATION_DEFAULT = re.compile(r"\$\{[A-Z0-9_]+:-([^}]*)\}")
SEMVER_TAG = re.compile(r":\d+\.\d+\.\d+$")
MODELS = ("qwen2.5:7b-instruct", "qwen2.5vl:7b", "nomic-embed-text")
SINGLE_HOST_OVERLAY = "deploy/single-host/compose.override.yaml"
LLM_NETWORK = "paper-trail-llm"
COMPOSE_FILES = ("compose.yaml", "compose.dev.yaml", "deploy/llm/compose.yaml", SINGLE_HOST_OVERLAY)


def _services(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = data["services"]
    return services


def _port_strings(service: dict[str, Any]) -> list[str]:
    ports = service.get("ports", [])
    assert all(isinstance(p, str) for p in ports), "use the short string port syntax"
    return list(ports)


def _default_port_mapping(port: str) -> tuple[str, int, int]:
    """Resolve ${VAR:-default} to its default and split host:published:container."""
    resolved = INTERPOLATION_DEFAULT.sub(r"\1", port)
    host, published, container = resolved.split(":")
    return host, int(published), int(container)


def _argv(service: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("entrypoint", "command"):
        value = service.get(key, [])
        parts.extend([value] if isinstance(value, str) else map(str, value))
    return " ".join(parts)


def _environment(service: dict[str, Any]) -> dict[str, str]:
    environment = service.get("environment", {})
    if isinstance(environment, list):
        return dict(item.split("=", 1) for item in environment)
    return {key: str(value) for key, value in environment.items()}


# --- S0-AC-07 --------------------------------------------------------------------------------


def test_s0_ac_07_project_name_is_paper_trail(compose: dict[str, Any]) -> None:
    """S0-AC-07 / PLAN 1.1: the compose project is named paper-trail."""
    assert compose["name"] == "paper-trail"


def test_s0_ac_07_runtime_services_share_one_image(compose: dict[str, Any]) -> None:
    """S0-AC-07: migrate, web, worker (and the agent core's on-demand cli) all use
    paper-trail:latest (one image, ADR-0003)."""
    services = _services(compose)

    assert set(services) == RUNTIME_SERVICES | ON_DEMAND_SERVICES
    for name in RUNTIME_SERVICES | ON_DEMAND_SERVICES:
        assert services[name]["image"] == APP_IMAGE, name


def test_s0_ac_07_image_is_built_from_the_runtime_target(compose: dict[str, Any]) -> None:
    """S0-AC-07: the image is built from Dockerfile target `runtime`, exactly once."""
    builders = {n: s["build"] for n, s in _services(compose).items() if "build" in s}

    assert builders, "one runtime service must declare how the image is built"
    for build in builders.values():
        assert build["target"] == "runtime"
        assert build.get("context", ".") == "."
        assert "GIT_SHA" in build.get("args", {})


def test_s0_ac_07_runtime_services_have_no_profiles(compose: dict[str, Any]) -> None:
    """S0-AC-07: `make up` starts every runtime service (none hidden behind a profile)."""
    services = _services(compose)
    for name in RUNTIME_SERVICES:
        assert "profiles" not in services[name], name


def test_s0_ac_07_commands_and_startup_order(compose: dict[str, Any]) -> None:
    """S0-AC-07 / PLAN 2.4: migrate runs `db upgrade` once; web/worker wait for it."""
    services = _services(compose)

    assert services["migrate"]["command"] == ["db", "upgrade"]
    assert services["migrate"]["restart"] == "no"
    assert services["web"]["command"] == ["serve"]
    assert services["worker"]["command"] == ["worker"]
    for name in ("web", "worker"):
        condition = services[name]["depends_on"]["migrate"]["condition"]
        assert condition == "service_completed_successfully", name


def test_s0_ac_07_worker_has_heartbeat_healthcheck(compose: dict[str, Any]) -> None:
    """S0-AC-07 / S0-AC-24: worker health = `paper-trail worker --check-heartbeat`."""
    check = _services(compose)["worker"]["healthcheck"]

    assert check["test"] == ["CMD", "paper-trail", "worker", "--check-heartbeat"]


def test_s0_ac_36_llm_stack_network_has_a_fixed_name(compose_llm: dict[str, Any]) -> None:
    """S0-AC-36 / PLAN 2.4a: the LLM project's default network is named paper-trail-llm, so the
    app stack on the same machine can join it."""
    assert compose_llm["networks"]["default"]["name"] == LLM_NETWORK
    for name, service in _services(compose_llm).items():
        assert "networks" not in service, f"{name} must stay on the default (named) network"


def test_s0_ac_36_single_host_overlay_joins_worker_to_the_llm_network() -> None:
    """S0-AC-36 / PLAN 2.4a: the single-machine overlay attaches only the worker to the existing
    LLM network (external, same default name), keeping it on its own default network too."""
    overlay = load_yaml(SINGLE_HOST_OVERLAY)
    services = _services(overlay)

    assert set(services) == {"worker"}
    assert set(services["worker"]) == {"networks"}
    assert services["worker"]["networks"] == ["default", "llm"]
    network = overlay["networks"]["llm"]
    assert network["external"] is True
    assert INTERPOLATION_DEFAULT.sub(r"\1", network["name"]) == LLM_NETWORK


def test_s0_ac_36_runtime_stack_does_not_rely_on_host_gateway(compose: dict[str, Any]) -> None:
    """PLAN 2.4a: host.docker.internal cannot reach a port published on 127.0.0.1 on Linux, so
    the runtime stack does not offer it as the single-machine route."""
    for name, service in _services(compose).items():
        assert "extra_hosts" not in service, name


def test_s0_ac_07_runtime_services_use_the_data_volume_and_timezone(
    compose: dict[str, Any],
) -> None:
    """S0-AC-07 / PLAN 1.3, 2.4: /data volume, TZ America/Toronto (Windsor, Ontario)."""
    for name, service in _services(compose).items():
        assert "${PAPER_TRAIL_DATA_VOLUME:-paper-trail-data}:/data" in service["volumes"], name
        assert _environment(service)["TZ"] == "America/Toronto", name
        assert _environment(service)["PAPER_TRAIL_DATA_DIR"] == "/data", name
        assert "user" not in service, f"{name} must run as the image's app user"
    assert "paper-trail-data" in compose["volumes"]


def test_s0_ac_07_dev_tooling_only_in_dev_compose_under_dev_profile(
    compose_dev: dict[str, Any],
) -> None:
    """S0-AC-07: dev tooling services live in compose.dev.yaml under profile `dev`."""
    services = _services(compose_dev)

    assert set(services) >= DEV_SERVICES
    assert not set(services) & RUNTIME_SERVICES, "dev compose must not redefine runtime services"
    for name, service in services.items():
        assert "dev" in service.get("profiles", []), name


def test_s0_ac_07_dev_services_run_as_host_uid(compose_dev: dict[str, Any]) -> None:
    """S0-AC-07 / PLAN 2.4: tooling runs as ${PAPER_TRAIL_UID}:${PAPER_TRAIL_GID}, HOME=/tmp."""
    for name in ("dev", "frontend", "vite"):
        service = _services(compose_dev)[name]
        assert service["user"] == "${PAPER_TRAIL_UID:-1000}:${PAPER_TRAIL_GID:-1000}", name
        assert _environment(service)["HOME"] == "/tmp", name  # noqa: S108


def test_s0_ac_07_dev_services_build_the_right_targets(compose_dev: dict[str, Any]) -> None:
    """S0-AC-07 / PLAN 2.4: dev -> python-dev, frontend/vite -> frontend-dev."""
    services = _services(compose_dev)

    assert services["dev"]["build"]["target"] == "python-dev"
    assert services["frontend"]["build"]["target"] == "frontend-dev"
    assert services["vite"]["build"]["target"] == "frontend-dev"
    assert ".:/app" in services["dev"]["volumes"]
    assert "frontend_node_modules:/app/frontend/node_modules" in services["frontend"]["volumes"]


# --- S0-AC-10 --------------------------------------------------------------------------------


def test_s0_ac_10_web_binds_only_the_configured_address(compose: dict[str, Any]) -> None:
    """S0-AC-10: web publishes BIND_ADDR:PORT:8000, defaulting to 127.0.0.1:8420."""
    services = _services(compose)

    assert _port_strings(services["web"]) == [f"{BIND}:${{PAPER_TRAIL_PORT:-8420}}:8000"]
    assert _port_strings(services["migrate"]) == []
    assert _port_strings(services["worker"]) == []


def test_s0_ac_10_vite_binds_loopback_by_default(compose_dev: dict[str, Any]) -> None:
    """S0-AC-10 / PLAN 1.1: the dev server binds ${PAPER_TRAIL_BIND_ADDR:-127.0.0.1}:5173."""
    assert _port_strings(_services(compose_dev)["vite"]) == [f"{BIND}:5173:5173"]


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_s0_ac_10_nothing_publishes_on_all_interfaces(compose_file: str) -> None:
    """S0-AC-10: every published port names a host address; never 0.0.0.0 or an implicit one."""
    data = load_yaml(compose_file)
    for name, service in _services(data).items():
        assert service.get("network_mode") != "host", name
        for port in _port_strings(service):
            host, _, _ = _default_port_mapping(port)
            assert host == "127.0.0.1", f"{name}: {port} must default to loopback"


def test_s0_ac_10_default_ports_avoid_host_services(
    compose: dict[str, Any], compose_dev: dict[str, Any], compose_llm: dict[str, Any]
) -> None:
    """S0-AC-10: default host ports (8420, 8421 for MCP, 5173, 11434) are not in the PLAN 1.1
    in-use list."""
    defaults: set[int] = set()
    for data in (compose, compose_dev, compose_llm):
        for service in _services(data).values():
            defaults.update(_default_port_mapping(p)[1] for p in _port_strings(service))

    assert defaults == {8420, 8421, 5173, 11434}
    assert not defaults & HOST_PORTS_IN_USE


# --- S0-AC-12 --------------------------------------------------------------------------------


@pytest.mark.parametrize("service_name", sorted(RUNTIME_SERVICES))
def test_s0_ac_12_runtime_services_are_hardened(compose: dict[str, Any], service_name: str) -> None:
    """S0-AC-12: read_only, /tmp tmpfs, no-new-privileges, cap_drop ALL, json-file rotation."""
    service = _services(compose)[service_name]

    assert service["read_only"] is True
    assert "/tmp" in service["tmpfs"]  # noqa: S108
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["cap_drop"] == ["ALL"]
    assert "cap_add" not in service
    assert service.get("privileged") is not True
    logging = service["logging"]
    assert logging["driver"] == "json-file"
    assert logging["options"]["max-size"]
    assert logging["options"]["max-file"]
    assert service["restart"] in {"unless-stopped", "no"}


@pytest.mark.parametrize("service_name", ["web", "worker"])
def test_s0_ac_12_long_running_services_have_resource_limits(
    compose: dict[str, Any], service_name: str
) -> None:
    """S0-AC-12: memory and CPU limits protect the other self-hosted services (media server, DNS,
    home automation) on the shared host."""
    service = _services(compose)[service_name]

    assert service["mem_limit"]
    assert float(service["cpus"]) > 0


def test_s0_ac_12_worker_stop_grace_exceeds_shutdown_grace(compose: dict[str, Any]) -> None:
    """S0-AC-12 / S0-AC-25: compose waits longer than WORKER__SHUTDOWN_GRACE_S (8 s) + 1 s."""
    grace = _services(compose)["worker"]["stop_grace_period"]

    assert re.fullmatch(r"(\d+)s", grace)
    assert int(grace[:-1]) > 9


# --- S0-AC-37 --------------------------------------------------------------------------------


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_s0_ac_37_images_are_pinned_never_latest(compose_file: str) -> None:
    """S0-AC-37: every `image:` has an explicit version tag; only paper-trail may use latest."""
    for name, service in _services(load_yaml(compose_file)).items():
        image = service.get("image")
        if image is None or image == APP_IMAGE:
            continue
        assert PINNED_TAG.match(image), f"{compose_file}:{name} image {image!r} is not pinned"
        assert not image.endswith(":latest"), f"{compose_file}:{name}"


# --- S0-AC-39 --------------------------------------------------------------------------------


def test_s0_ac_39_llm_services(compose_llm: dict[str, Any]) -> None:
    """S0-AC-39: the LLM host runs `ollama` and a one-shot `ollama-init`."""
    assert set(_services(compose_llm)) == {"ollama", "ollama-init"}


def test_s0_ac_39_ollama_image_is_pinned_to_an_exact_version(compose_llm: dict[str, Any]) -> None:
    """S0-AC-39 / S0-AC-37: ollama/ollama is pinned to x.y.z (both services, same image)."""
    services = _services(compose_llm)
    image = services["ollama"]["image"]

    assert image.startswith("ollama/ollama:")
    assert SEMVER_TAG.search(image), image
    assert services["ollama-init"]["image"] == image


def test_s0_ac_39_ollama_reserves_the_nvidia_gpu(compose_llm: dict[str, Any]) -> None:
    """S0-AC-39: deploy.resources.reservations.devices requests an NVIDIA GPU."""
    devices = _services(compose_llm)["ollama"]["deploy"]["resources"]["reservations"]["devices"]

    assert any(
        d.get("driver") == "nvidia" and "gpu" in d.get("capabilities", []) for d in devices
    ), devices


def test_s0_ac_39_models_live_in_a_named_volume(compose_llm: dict[str, Any]) -> None:
    """S0-AC-39: models persist in a named volume mounted at /root/.ollama."""
    volumes = _services(compose_llm)["ollama"]["volumes"]
    (mount,) = [v for v in volumes if v.endswith(":/root/.ollama")]
    source = mount.split(":", 1)[0]

    assert not source.startswith((".", "/", "$")), "must be a named volume"
    assert source in compose_llm["volumes"]


def test_s0_ac_39_ollama_port_bound_to_configurable_address(compose_llm: dict[str, Any]) -> None:
    """S0-AC-39: 11434 is published only on ${OLLAMA_BIND_ADDR:-127.0.0.1}."""
    services = _services(compose_llm)

    assert _port_strings(services["ollama"]) == ["${OLLAMA_BIND_ADDR:-127.0.0.1}:11434:11434"]
    assert _port_strings(services["ollama-init"]) == []


def test_s0_ac_39_init_is_one_shot_after_ollama(compose_llm: dict[str, Any]) -> None:
    """S0-AC-39: ollama-init waits for ollama, pulls the models and exits (no restart)."""
    init = _services(compose_llm)["ollama-init"]

    assert init.get("restart", "no") == "no"
    assert "ollama" in init["depends_on"]
    assert "deploy" not in init, "the init job needs no GPU"


def test_s0_ac_39_llm_env_example_documents_bind_addr_and_models() -> None:
    """S0-AC-39: deploy/llm/.env.example documents OLLAMA_BIND_ADDR and the three models."""
    text = read_text("deploy/llm/.env.example")

    assert re.search(r"^#?\s*OLLAMA_BIND_ADDR=", text, re.MULTILINE)
    for model in MODELS:
        assert model in text, model


def test_s0_ac_39_init_pulls_every_configured_model() -> None:
    """S0-AC-39: the ollama-init definition references each model (literal or ${VAR:-default})."""
    init_text = read_text("deploy/llm/compose.yaml").split("ollama-init:", 1)[1]

    for model in MODELS:
        assert model in init_text, f"ollama-init does not pull {model}"


# --- S0-AC-11 / S0-AC-34: smoke and container-check runners -----------------------------------


def test_s0_ac_11_smoke_service_runs_the_smoke_script(compose_dev: dict[str, Any]) -> None:
    """S0-AC-11: the curl `smoke` service runs scripts/smoke.sh against http://web:8000."""
    smoke = _services(compose_dev)["smoke"]

    assert smoke["image"].startswith("curlimages/curl:")
    assert "./scripts:/scripts:ro" in smoke["volumes"]
    assert "/scripts/smoke.sh" in _argv(smoke)


def test_s0_ac_34_container_checks_service_uses_the_docker_cli(compose_dev: dict[str, Any]) -> None:
    """S0-AC-34: container checks run in a pinned docker CLI image with the socket mounted and
    the repo mounted at its host path (so compose bind paths resolve on the host daemon)."""
    service = _services(compose_dev)["container-checks"]

    assert service["image"].startswith("docker:")
    assert "/var/run/docker.sock:/var/run/docker.sock" in service["volumes"]
    workdir = service["working_dir"]
    assert any(v.startswith(f"{workdir}:{workdir}") for v in service["volumes"])
    assert "tests/container/run.sh" in _argv(service)


def test_s0_ac_34_gitleaks_scans_the_repo_read_only(compose_dev: dict[str, Any]) -> None:
    """S0-AC-04/34: the gitleaks service mounts the repo read-only and runs the scan script,
    whose default mode covers history and the working tree."""
    gitleaks = _services(compose_dev)["gitleaks"]

    assert gitleaks["image"].startswith("zricethezav/gitleaks:")
    assert ".:/repo:ro" in gitleaks["volumes"]
    assert _argv(gitleaks) == "/bin/sh /repo/scripts/secrets-scan.sh all"


def test_s0_ac_34_gitleaks_scan_covers_history_tree_and_staged_changes() -> None:
    """S0-AC-04/34: `all` runs `gitleaks git` (history) and `gitleaks dir` (working tree),
    `staged` runs `gitleaks git --staged`; the deprecated `detect` (history only) is not used."""
    script = read_text("scripts/secrets-scan.sh")

    assert re.search(r"^\s*scan git \.$", script, re.MULTILINE)
    assert re.search(r"^\s*scan dir \.$", script, re.MULTILINE)
    assert re.search(r"^\s*scan git --staged \.$", script, re.MULTILINE)
    assert "--exit-code 1" in script
    assert "detect" not in script
    assert "protect" not in script
