"""Health domain types and the CheckReadiness use case (S0-AC-18)."""

import dataclasses

import pytest

from paper_trail.application.health import CheckReadiness, ReadinessReport
from paper_trail.domain.health import ComponentHealth, HealthStatus
from tests.fakes.health import StaticHealthProbe


def test_s0_ac_18_health_status_values_are_lowercase_names() -> None:
    """S0-AC-18: HealthStatus is a StrEnum whose values are used verbatim in the API."""
    assert [status.value for status in HealthStatus] == ["ok", "degraded", "error"]
    assert HealthStatus.OK == "ok"  # type: ignore[comparison-overlap]  # StrEnum vs str at runtime


def test_s0_ac_18_component_health_is_immutable_with_empty_defaults() -> None:
    """S0-AC-18: ComponentHealth is a frozen value object; detail and data default to empty."""
    component = ComponentHealth(name="database", status=HealthStatus.OK)

    assert component.detail is None
    assert dict(component.data) == {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        component.status = HealthStatus.ERROR  # type: ignore[misc]


@pytest.mark.parametrize(
    ("statuses", "ready"),
    [
        ((), True),
        ((HealthStatus.OK,), True),
        ((HealthStatus.OK, HealthStatus.DEGRADED), True),
        ((HealthStatus.DEGRADED,), True),
        ((HealthStatus.ERROR,), False),
        ((HealthStatus.OK, HealthStatus.ERROR), False),
        ((HealthStatus.DEGRADED, HealthStatus.ERROR, HealthStatus.OK), False),
    ],
)
def test_s0_ac_18_ready_iff_no_component_is_error(
    statuses: tuple[HealthStatus, ...], ready: bool
) -> None:
    """S0-AC-18: CheckReadiness is ready iff no component reports ERROR."""
    probes = [StaticHealthProbe(f"probe-{i}", status) for i, status in enumerate(statuses)]

    report = CheckReadiness(probes)()

    assert isinstance(report, ReadinessReport)
    assert report.ready is ready


def test_s0_ac_18_report_lists_components_in_probe_order() -> None:
    """S0-AC-18: the report carries every probe's ComponentHealth, in order, as a tuple."""
    probes = [
        StaticHealthProbe("database", HealthStatus.OK, data={"revision": "0001"}),
        StaticHealthProbe("disk", HealthStatus.ERROR, detail="read-only"),
    ]

    report = CheckReadiness(probes)()

    assert isinstance(report.components, tuple)
    assert [c.name for c in report.components] == ["database", "disk"]
    assert report.components[0].data == {"revision": "0001"}
    assert report.components[1].detail == "read-only"


def test_s0_ac_18_every_probe_is_checked_on_every_call() -> None:
    """S0-AC-18: readiness is computed fresh on each call (no caching of a stale result)."""
    probe = StaticHealthProbe("database", HealthStatus.OK)
    check = CheckReadiness([probe])

    check()
    check()

    assert probe.calls == 2
