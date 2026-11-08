"""HealthProbe port contract, run against the fake and the real adapter (S0-AC-18, PLAN 1.7)."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.health import DatabaseHealthProbe
from paper_trail.domain.health import ComponentHealth, HealthStatus
from paper_trail.ports.health import HealthProbe
from tests.fakes.health import StaticHealthProbe

ProbeFactory = Callable[[Path], Iterator[HealthProbe]]


def _static(_: Path) -> Iterator[HealthProbe]:
    yield StaticHealthProbe("static", HealthStatus.OK)


def _database(tmp_path: Path) -> Iterator[HealthProbe]:
    engine = create_engine(tmp_path / "paper_trail.db")
    try:
        yield DatabaseHealthProbe(engine)
    finally:
        engine.dispose()


FACTORIES: dict[str, ProbeFactory] = {"static": _static, "database": _database}


@pytest.fixture(params=sorted(FACTORIES))
def probe(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[HealthProbe]:
    yield from FACTORIES[request.param](tmp_path)


def test_s0_ac_18_probe_has_a_non_empty_name(probe: HealthProbe) -> None:
    """HealthProbe contract: name is a non-empty string."""
    assert isinstance(probe.name, str)
    assert probe.name


def test_s0_ac_18_check_returns_component_health_for_its_name(probe: HealthProbe) -> None:
    """HealthProbe contract: check() returns ComponentHealth named after the probe, never raises."""
    health = probe.check()

    assert isinstance(health, ComponentHealth)
    assert health.name == probe.name
    assert isinstance(health.status, HealthStatus)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in health.data.items())
