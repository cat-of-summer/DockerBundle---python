from __future__ import annotations

import pytest

from core.model import MountSpec, Origin, PortSpec, ServiceSpec
from recipes import fallback, schema
from recipes.match import Registry


def spec(slug: str, **kwargs) -> ServiceSpec:
    base = {"slug": slug, "name": slug, "package": slug, "origin": Origin.CATALOG}
    base.update(kwargs)
    return ServiceSpec(**base)


def test_builtin_recipes_all_load(registry: Registry):
    assert not registry.warnings
    assert "nginx" in registry.recipes
    assert "traefik" in registry.recipes


def test_mismatching_image_vetoes_but_a_missing_one_does_not():
    match = schema.RecipeMatch(image=["php:*fpm*"], service=["php"])
    # A Postgres container must never be claimed by the php recipe.
    assert match.score(image="postgres:16", files=set(), command="", service="php") == 0
    # With no image at all, the name still counts.
    assert match.score(image="", files=set(), command="", service="php") == 20


def test_image_match_outranks_weaker_signals():
    match = schema.RecipeMatch(image=["nginx:*"])
    assert match.score(image="nginx:alpine", files=set(), command="", service="x") == 100


def test_package_is_used_to_disambiguate():
    match = schema.RecipeMatch(image=["node:*"], package=["*backend*"])
    backend = match.score(
        image="node:lts", files=set(), command="", service="node", package="node(backend)-nginx"
    )
    frontend = match.score(
        image="node:lts", files=set(), command="", service="node", package="vue-nginx-vite"
    )
    assert backend > frontend


def test_unknown_kind_in_mount_kinds_is_rejected():
    with pytest.raises(schema.RecipeError, match="mount_kinds"):
        schema.from_dict({"name": "x", "mount_kinds": {"/a": "nonsense"}})


def test_unknown_port_mechanism_is_rejected():
    with pytest.raises(schema.RecipeError, match="configure.type"):
        schema.from_dict({"name": "x", "port": {"default": 1, "configure": {"type": "magic"}}})


def test_program_without_a_command_is_rejected():
    with pytest.raises(schema.RecipeError, match="needs a command"):
        schema.from_dict({"name": "x", "supervisor": [{"name": "p"}]})


def test_fallback_runs_the_images_own_command():
    service = spec(
        "svc",
        image="ghcr.io/acme/svc:1",
        entrypoint=["/entrypoint.sh"],
        command=["serve", "--verbose"],
        ports=[PortSpec(container=8080, original=8080)],
    )
    recipe = fallback.build(service)
    assert recipe.programs[0].command == "/entrypoint.sh serve --verbose"
    # A port-bound import must not be replicated, and its port cannot be moved.
    assert recipe.port.mechanism == "none"
    assert not recipe.programs[0].scalable


def test_fallback_without_a_command_fails_loudly():
    recipe = fallback.build(spec("svc", image="x:1"))
    assert "exit 1" in recipe.programs[0].command


def test_fallback_marks_docker_socket_services_unbakeable():
    service = spec(
        "traefik",
        image="traefik:v3",
        mounts=[MountSpec(source="/var/run/docker.sock", target="/var/run/docker.sock")],
    )
    recipe = fallback.build(service)
    assert not recipe.bakeable
    assert "Docker socket" in recipe.reason


def test_registry_falls_back_for_an_unknown_image(registry: Registry):
    recipe, is_fallback = registry.resolve(spec("weird", image="clickhouse/clickhouse-server:24"))
    assert is_fallback
    assert recipe.name.startswith("auto:")


def test_forced_recipe_that_does_not_exist_raises(registry: Registry):
    with pytest.raises(KeyError):
        registry.resolve(spec("a", image="nginx:alpine"), forced="no-such-recipe")


def test_traefik_is_not_bakeable(registry: Registry):
    recipe, _ = registry.resolve(spec("traefik", image="traefik:v3"))
    assert recipe.name == "traefik"
    assert not recipe.bakeable
