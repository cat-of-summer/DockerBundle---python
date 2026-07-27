from __future__ import annotations

from core.model import Origin, PortSpec, ServiceSpec
from plan import ports
from recipes.schema import PortRule, Recipe


def service(slug: str, port: int | None = None) -> ServiceSpec:
    spec = ServiceSpec(slug=slug, name=slug, package=slug, origin=Origin.COMPOSE)
    if port is not None:
        spec.ports = [PortSpec(container=port, original=port, published=f"127.0.0.1:{port}")]
    return spec


def recipe(name: str, *, mechanism: str = "cli_flag", default: int = 0) -> Recipe:
    return Recipe(name=name, port=PortRule(default=default, mechanism=mechanism))


def test_first_claimant_keeps_the_port_and_the_second_moves():
    result = ports.allocate(
        [(service("a", 80), recipe("nginx")), (service("b", 80), recipe("nginx"))],
        port_range=(20000, 20010),
    )
    assert result.for_service("a")[0].container == 80
    assert result.for_service("b")[0].container == 20000
    assert result.for_service("b")[0].remapped
    assert not result.errors
    assert "moved to 20000" in result.warnings[0]


def test_unmovable_service_reports_an_error_rather_than_a_broken_image():
    # A rootfs-imported service has no known place to change its port.
    result = ports.allocate(
        [
            (service("a", 6379), recipe("redis")),
            (service("b", 6379), recipe("auto", mechanism="none")),
        ],
        port_range=(20000, 20010),
    )
    assert result.errors
    assert "cannot relocate it" in result.errors[0]


def test_pinned_ports_win_and_are_reserved_first():
    # A committed manifest must keep producing the same image, so a pin beats
    # declaration order even when the pinned service comes second.
    result = ports.allocate(
        [(service("a", 80), recipe("nginx")), (service("b", 80), recipe("nginx"))],
        pinned={"b": {80: 80}},
        port_range=(20000, 20010),
    )
    assert result.for_service("b")[0].container == 80
    assert result.for_service("a")[0].container == 20000


def test_recipe_default_used_when_compose_declares_no_port():
    result = ports.allocate([(service("a"), recipe("php", default=9000))])
    assert result.for_service("a")[0].container == 9000


def test_exhausted_range_is_an_error():
    entries = [(service(f"s{i}", 80), recipe("nginx")) for i in range(4)]
    result = ports.allocate(entries, port_range=(20000, 20001))
    assert any("no free port left" in error for error in result.errors)
