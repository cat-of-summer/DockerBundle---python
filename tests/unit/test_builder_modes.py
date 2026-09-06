"""``mode:``, feature flags and the collisions that now stop a build.

Everything here is about which services take part in a bundle and what happens when two
of them want the same name.
"""

from __future__ import annotations

import pytest

from core.manifest import EnvRule, Manifest, ServiceEntry, SourceRef
from core.model import ServiceMode
from discover import resolve
from plan import builder
from plan.builder import PlanError

ALL = {
    "mysql",
    "laravel_nginx_laravel",
    "laravel_nginx_nginx",
    "vue_nginx_vite_vue",
    "vue_nginx_vite_nginx",
}
RESOLVED = {"EXTERNAL_ACCESS": "prefix", "VITE_API_BASE_URL": "value:/api"}


def specs_for(catalog, slugs):
    discovery = resolve.collect([SourceRef(type="catalog", path=str(catalog))], catalog.parent)
    return [spec for spec in discovery.services if spec.slug in slugs]


def plan_for(catalog, registry, slugs, *, services=None, features=None, env=None, **kwargs):
    specs = specs_for(catalog, slugs)
    manifest = Manifest(
        name="test",
        env=dict(RESOLVED) if env is None else env,
        services=services or {slug: ServiceEntry(slug=slug) for slug in slugs},
        **kwargs,
    )
    return builder.build(specs, manifest, registry, features=features)


def catalogue(root, folder, service, image):
    """A one-package catalogue holding a single service."""
    package = root / folder
    package.mkdir(parents=True)
    (package / "docker-compose.yml").write_text(
        f"services:\n  {service}:\n    image: {image}\n", encoding="utf-8"
    )
    return root


# -- mode -------------------------------------------------------------------


def test_mode_off_leaves_the_service_out_entirely(catalog, registry):
    plan = plan_for(
        catalog,
        registry,
        ALL,
        services={
            **{slug: ServiceEntry(slug=slug) for slug in ALL},
            "mysql": ServiceEntry(slug="mysql", mode=ServiceMode.OFF),
        },
    )
    assert "mysql" not in {s.spec.slug for s in plan.services}
    # Its keys go with it.
    assert not any(entry.source == "mysql" for entry in plan.env)


def test_mode_external_keeps_the_keys_but_builds_nothing(catalog, registry):
    plan = plan_for(
        catalog,
        registry,
        ALL,
        services={
            **{slug: ServiceEntry(slug=slug) for slug in ALL},
            "mysql": ServiceEntry(slug="mysql", mode=ServiceMode.EXTERNAL),
        },
    )
    assert "mysql" not in {s.spec.slug for s in plan.baked}
    assert "mysql" not in {s.spec.slug for s in plan.sidecars}
    assert [spec.slug for spec in plan.external] == ["mysql"]
    # The whole point: the deployment can still say where the database actually is.
    assert any(entry.source == "mysql" for entry in plan.env)


def test_mode_sidecar_moves_a_bakeable_service_out_of_the_image(catalog, registry):
    plan = plan_for(
        catalog,
        registry,
        ALL,
        services={
            **{slug: ServiceEntry(slug=slug) for slug in ALL},
            "mysql": ServiceEntry(slug="mysql", mode=ServiceMode.SIDECAR),
        },
    )
    assert "mysql" in {s.spec.slug for s in plan.sidecars}
    assert "mysql" not in {s.spec.slug for s in plan.baked}


def test_an_unbakeable_recipe_still_defaults_to_a_sidecar(catalog, registry):
    plan = plan_for(catalog, registry, {*ALL, "traefik"})
    assert "traefik" in {s.spec.slug for s in plan.sidecars}


def test_mode_bake_overrules_a_recipe_that_asked_for_a_sidecar(catalog, registry):
    plan = plan_for(
        catalog,
        registry,
        {*ALL, "traefik"},
        services={
            **{slug: ServiceEntry(slug=slug) for slug in ALL},
            "traefik": ServiceEntry(slug="traefik", mode=ServiceMode.BAKE),
        },
    )
    assert "traefik" in {s.spec.slug for s in plan.baked}


def test_switching_everything_off_is_an_error_not_an_empty_image(catalog, registry):
    with pytest.raises(PlanError, match="nothing is left to build"):
        plan_for(
            catalog,
            registry,
            ALL,
            services={slug: ServiceEntry(slug=slug, mode=ServiceMode.OFF) for slug in ALL},
        )


# -- features ---------------------------------------------------------------


def test_a_feature_decides_whether_a_service_is_in_the_image(catalog, registry):
    services = {
        **{slug: ServiceEntry(slug=slug) for slug in ALL},
        "mysql": ServiceEntry(slug="mysql", when=["mysql"]),
    }

    on = plan_for(catalog, registry, ALL, services=services, features={"mysql": True})
    assert "mysql" in {s.spec.slug for s in on.baked}

    off = plan_for(catalog, registry, ALL, services=services, features={"mysql": False})
    assert "mysql" not in {s.spec.slug for s in off.services}


def test_the_flags_a_plan_was_built_with_are_recorded(catalog, registry):
    plan = plan_for(catalog, registry, ALL, features={"mysql": True, "gpu": False})
    assert plan.features == {"mysql": True, "gpu": False}


def test_command_line_flags_beat_the_file(catalog, registry):
    from core import features as features_mod

    manifest = Manifest(name="test", features={"mysql": True})
    resolved = features_mod.apply_overrides(manifest.features, enable=[], disable=["mysql"])
    plan = plan_for(catalog, registry, ALL, features=resolved)
    assert plan.features == {"mysql": False}


# -- collisions -------------------------------------------------------------


def test_two_different_services_on_one_slug_are_refused(tmp_path):
    # A service plainly called `web` exists in most of the stands this tool merges;
    # keeping whichever source came first would drop the other without a word.
    first = catalogue(tmp_path / "a", "web", "nginx", "nginx:alpine")
    second = catalogue(tmp_path / "b", "web", "redis", "redis:7-alpine")

    discovery = resolve.collect(
        [
            SourceRef(type="catalog", path=str(first)),
            SourceRef(type="catalog", path=str(second)),
        ],
        tmp_path,
    )
    assert any("prefix" in error for error in discovery.errors)


def test_the_same_service_described_twice_is_not_a_collision(tmp_path):
    # A catalogue folder and the container running out of it legitimately meet here.
    first = catalogue(tmp_path / "a", "web", "nginx", "nginx:alpine")
    second = catalogue(tmp_path / "b", "web", "nginx", "nginx:alpine")

    discovery = resolve.collect(
        [
            SourceRef(type="catalog", path=str(first)),
            SourceRef(type="catalog", path=str(second)),
        ],
        tmp_path,
    )
    assert not discovery.errors
    assert [spec.slug for spec in discovery.services] == ["web"]
    assert discovery.warnings


def test_a_source_prefix_settles_a_slug_collision(tmp_path):
    first = catalogue(tmp_path / "a", "web", "nginx", "nginx:alpine")
    second = catalogue(tmp_path / "b", "web", "redis", "redis:7-alpine")

    discovery = resolve.collect(
        [
            SourceRef(type="catalog", path=str(first)),
            SourceRef(type="catalog", path=str(second), prefix="other"),
        ],
        tmp_path,
    )
    assert not discovery.errors
    assert {spec.slug for spec in discovery.services} == {"web", "other_web"}


def test_a_declared_volume_cannot_take_a_name_a_service_already_uses(catalog, registry):
    with pytest.raises(PlanError, match="Rename it"):
        plan_for(catalog, registry, ALL, volumes={"mysql_data": "/somewhere/else"})


def test_local_pointing_outside_the_bundle_blocks_generation(catalog, registry):
    with pytest.raises(PlanError, match="local:ghost"):
        plan_for(
            catalog,
            registry,
            ALL,
            env={**RESOLVED, "MYSQL_VERSION": EnvRule(rule="local:ghost")},
        )
