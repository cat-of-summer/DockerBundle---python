"""End-to-end planning against the sample catalogue, without touching Docker."""

from __future__ import annotations

import pytest

from core.manifest import Manifest, ServiceEntry, SourceRef
from core.model import MountMode
from discover import resolve
from plan import builder
from plan.builder import INIT_BARRIER, PlanError


def plan_for(catalog, registry, slugs, **manifest_kwargs):
    discovery = resolve.collect([SourceRef(type="catalog", path=str(catalog))], catalog.parent)
    specs = [spec for spec in discovery.services if spec.slug in slugs]
    assert len(specs) == len(slugs), sorted(s.slug for s in discovery.services)

    manifest = Manifest(
        name="test",
        services={spec.slug: ServiceEntry(slug=spec.slug) for spec in specs},
        **manifest_kwargs,
    )
    return builder.build(specs, manifest, registry)


ALL = {
    "mysql",
    "laravel_nginx_laravel",
    "laravel_nginx_nginx",
    "vue_nginx_vite_vue",
    "vue_nginx_vite_nginx",
}
RESOLVED = {"env_conflicts": {"EXTERNAL_ACCESS": "prefix", "VITE_API_BASE_URL": "value:/api"}}


def test_unresolved_conflicts_block_generation(catalog, registry):
    with pytest.raises(PlanError) as excinfo:
        plan_for(catalog, registry, ALL)
    assert "EXTERNAL_ACCESS" in str(excinfo.value)


def test_full_plan(catalog, registry):
    plan = plan_for(catalog, registry, ALL, **RESOLVED)

    assert {s.spec.slug for s in plan.baked} == ALL
    assert not plan.sidecars

    names = [p.name for p in plan.programs]
    # One nginx master and one php-fpm master, however many services use them.
    assert names.count("nginx") == 1
    assert names.count("php-fpm") == 1
    assert "mysql" in names
    assert "queue-laravel_nginx_laravel" in names


def test_start_order_puts_the_database_first(catalog, registry):
    plan = plan_for(catalog, registry, ALL, **RESOLVED)
    priority = {p.name: p.priority for p in plan.programs}
    assert priority["mysql"] < priority["php-fpm"]
    assert priority["php-fpm"] < priority["nginx"]


def test_only_data_services_autostart_when_there_is_init(catalog, registry):
    plan = plan_for(catalog, registry, ALL, **RESOLVED)
    for program in plan.programs:
        assert program.autostart == (program.priority < INIT_BARRIER), program.name
    assert "nginx" in plan.deferred
    assert "mysql" not in plan.deferred


def test_colliding_nginx_ports_are_separated(catalog, registry):
    plan = plan_for(catalog, registry, ALL, **RESOLVED)
    assigned = {
        slug: [p.container for p in entries] for slug, entries in plan.port_map.items() if entries
    }
    assert assigned["laravel_nginx_nginx"] == [80]
    assert assigned["vue_nginx_vite_nginx"] != [80]
    # Every assigned port is unique across the bundle.
    flat = [port for ports in assigned.values() for port in ports]
    assert len(flat) == len(set(flat))


def test_traefik_is_kept_as_a_sidecar(catalog, registry):
    plan = plan_for(catalog, registry, {"mysql", "traefik"}, **RESOLVED)
    assert [s.spec.slug for s in plan.sidecars] == ["traefik"]
    assert [s.spec.slug for s in plan.baked] == ["mysql"]


def test_state_stays_a_volume_and_code_is_baked(catalog, registry):
    plan = plan_for(catalog, registry, ALL, **RESOLVED)
    assert plan.named_volumes == {"mysql_data": "/var/lib/mysql"}

    laravel = next(s for s in plan.baked if s.spec.slug == "laravel_nginx_laravel")
    targets = {copy.target for copy in laravel.copies}
    assert "/var/www/laravel_nginx_laravel" in targets


def test_each_source_file_is_copied_once(catalog, registry):
    # A recipe rule and a leftover mount must not both place the same file.
    plan = plan_for(catalog, registry, ALL, **RESOLVED)
    for service in plan.baked:
        sources = [copy.source for copy in service.copies]
        assert len(sources) == len(set(sources)), service.spec.slug


def test_service_entrypoints_are_preserved_verbatim(catalog, registry):
    plan = plan_for(catalog, registry, ALL, **RESOLVED)
    laravel = next(s for s in plan.baked if s.spec.slug == "laravel_nginx_laravel")
    assert laravel.init_script.endswith("entrypoint-laravel_nginx_laravel.sh")
    # It runs where the code was baked, not at the original /var/www/html.
    assert laravel.init_cwd == "/var/www/laravel_nginx_laravel"


def test_nginx_templates_are_staged_per_service(catalog, registry):
    plan = plan_for(catalog, registry, ALL, **RESOLVED)
    for slug in ("laravel_nginx_nginx", "vue_nginx_vite_nginx"):
        service = next(s for s in plan.baked if s.spec.slug == slug)
        assert any("/tmp/nginx.conf.template" in step for step in service.pre_init)
        assert any(f"conf.d/{slug}.conf" in step for step in service.post_init)


def test_replicas_on_a_port_bound_program_warn(catalog, registry):
    discovery = resolve.collect([SourceRef(type="catalog", path=str(catalog))], catalog.parent)
    specs = [s for s in discovery.services if s.slug == "mysql"]
    manifest = Manifest(services={"mysql": ServiceEntry(slug="mysql", replicas=3)})
    plan = builder.build(specs, manifest, registry)
    assert any("EADDRINUSE" in w for w in plan.warnings)


def test_queue_workers_are_scalable(catalog, registry):
    plan = plan_for(catalog, registry, ALL, **RESOLVED)
    queue = next(p for p in plan.programs if p.name.startswith("queue-"))
    assert queue.scalable
    assert queue.replicas_var.endswith("_REPLICAS")


def test_mount_override_is_honoured(catalog, registry):
    discovery = resolve.collect([SourceRef(type="catalog", path=str(catalog))], catalog.parent)
    specs = [s for s in discovery.services if s.slug == "mysql"]
    manifest = Manifest(
        services={"mysql": ServiceEntry(slug="mysql", mounts={"/var/lib/mysql": "copy"})}
    )
    plan = builder.build(specs, manifest, registry)
    mysql = plan.baked[0]
    assert all(m.mode is not MountMode.VOLUME for m in mysql.volumes) or not mysql.volumes


def test_env_prefixes_prefer_the_short_name(catalog, registry):
    plan = plan_for(catalog, registry, ALL, **RESOLVED)
    keys = {entry.key for entry in plan.env}
    # `laravel` is unique in the bundle, so it does not need the qualified slug.
    assert "LARAVEL_EXTERNAL_ACCESS" in keys
    # `nginx` appears twice, so those fall back to the full slug.
    assert "LARAVEL_NGINX_NGINX_EXTERNAL_ACCESS" in keys


def test_deferral_and_the_entrypoint_agree(catalog, registry):
    # If the planner defers a program but the renderer emits a single-phase entrypoint,
    # that program is never started at all.
    plan = plan_for(catalog, registry, ALL, **RESOLVED)
    assert plan.needs_init
    assert plan.deferred
    for program in plan.programs:
        assert program.autostart or plan.needs_init


def test_post_init_only_service_still_defers(catalog, registry):
    # nginx has no entrypoint of its own, only post_init that installs its config; it
    # must not start before that has run.
    plan = plan_for(catalog, registry, {"vue_nginx_vite_nginx", "mysql"}, **RESOLVED)
    nginx = next(p for p in plan.programs if p.name == "nginx")
    assert not nginx.autostart
    assert "nginx" in plan.deferred
