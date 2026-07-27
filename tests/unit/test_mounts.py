from __future__ import annotations

from core.model import MountKind, MountMode, MountSpec, Origin, ServiceSpec
from plan import mounts
from recipes.schema import Recipe


def spec_with(*entries: MountSpec) -> ServiceSpec:
    return ServiceSpec(
        slug="svc", name="svc", package="svc", origin=Origin.CATALOG, mounts=list(entries)
    )


def test_socket_is_never_baked():
    mount = mounts.classify_mount(
        MountSpec(source="/var/run/docker.sock", target="/var/run/docker.sock")
    )
    assert mount.kind is MountKind.SOCKET
    assert mount.mode is MountMode.SKIP


def test_named_volume_is_state():
    mount = mounts.classify_mount(MountSpec(source="vol", target="/whatever", named=True))
    assert mount.kind is MountKind.STATE
    assert mount.mode is MountMode.VOLUME


def test_state_directories_stay_volumes():
    for target in ("/var/lib/mysql", "/data", "/root/.ollama", "/qdrant/storage"):
        mount = mounts.classify_mount(MountSpec(source="./data", target=target))
        assert mount.kind is MountKind.STATE, target
        assert mount.mode is MountMode.VOLUME, target


def test_code_and_config_are_baked():
    code = mounts.classify_mount(MountSpec(source="./data", target="/var/www/html"))
    assert code.kind is MountKind.CODE
    assert code.mode is MountMode.COPY

    config = mounts.classify_mount(
        MountSpec(source="./php.ini", target="/usr/local/etc/php/conf.d/custom.ini")
    )
    assert config.kind is MountKind.CONFIG
    assert config.mode is MountMode.COPY


def test_unknown_targets_default_to_a_volume():
    # Baking something we do not understand risks freezing data into the image.
    mount = mounts.classify_mount(MountSpec(source="./x", target="/opt/mystery"))
    assert mount.kind is MountKind.UNKNOWN
    assert mount.mode is MountMode.VOLUME


def test_recipe_overrides_heuristics_and_longest_glob_wins():
    recipe = Recipe(
        name="r", mount_kinds={"/var/www/*": "code", "/var/www/html/storage": "state"}
    )
    code = mounts.classify_mount(MountSpec(source="a", target="/var/www/html"), recipe)
    state = mounts.classify_mount(MountSpec(source="b", target="/var/www/html/storage"), recipe)
    assert code.kind is MountKind.CODE
    assert state.kind is MountKind.STATE


def test_recipe_skip_drops_the_mount():
    recipe = Recipe(name="r", mount_kinds={"/etc/supervisord.conf": "skip"})
    mount = mounts.classify_mount(MountSpec(source="a", target="/etc/supervisord.conf"), recipe)
    assert mount.mode is MountMode.SKIP


def test_override_cannot_bake_a_socket():
    spec = spec_with(MountSpec(source="/var/run/docker.sock", target="/var/run/docker.sock"))
    mounts.classify(spec)
    warnings = mounts.apply_overrides(spec, {"/var/run/docker.sock": "copy"})
    assert warnings and "socket" in warnings[0]
    assert spec.mounts[0].mode is MountMode.SKIP


def test_override_of_a_missing_target_warns():
    spec = spec_with(MountSpec(source="./a", target="/a"))
    mounts.classify(spec)
    warnings = mounts.apply_overrides(spec, {"/nope": "copy"})
    assert warnings and "no mount at" in warnings[0]
