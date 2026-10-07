from __future__ import annotations

import pytest
import yaml

from core.manifest import EnvRule, Manifest, ManifestError, ServiceEntry, SourceRef
from core.model import ServiceMode

NAME = "docker-bundle.yml"


def test_round_trip_preserves_decisions(tmp_path):
    manifest = Manifest(
        name="shop",
        sources=[SourceRef(type="catalog", path="../packages")],
        env={"EXTERNAL_ACCESS": EnvRule(rule="prefix")},
        services={
            "mysql": ServiceEntry(slug="mysql", ports={3306: 3307}, replicas=1),
            "queue": ServiceEntry(slug="queue", replicas=4, mounts={"/data": "volume"}),
        },
    )
    path = manifest.save(tmp_path / NAME)
    reloaded = Manifest.load(path)

    assert reloaded.name == "shop"
    assert reloaded.env["EXTERNAL_ACCESS"].rule == "prefix"
    assert reloaded.services["mysql"].ports == {3306: 3307}
    assert reloaded.services["queue"].replicas == 4
    assert reloaded.services["queue"].mounts == {"/data": "volume"}
    assert reloaded.sources[0].path == "../packages"


def test_image_reference_round_trips(tmp_path):
    # Kept in the configuration so a plain `generate` cannot revert the published
    # reference.
    manifest = Manifest(name="stand", image="ghcr.io/acme/stand:latest")
    reloaded = Manifest.load(manifest.save(tmp_path / NAME))
    assert reloaded.image == "ghcr.io/acme/stand:latest"


def test_forced_labels_round_trip(tmp_path):
    manifest = Manifest(name="stand", labels={"traefik.enable": "${TRAEFIK_ENABLE:-true}"})
    reloaded = Manifest.load(manifest.save(tmp_path / NAME))
    assert reloaded.active_labels({}) == {"traefik.enable": "${TRAEFIK_ENABLE:-true}"}


def test_declared_volumes_round_trip(tmp_path):
    manifest = Manifest(name="stand", volumes={"artifacts": "/var/www/html/artifacts"})
    reloaded = Manifest.load(manifest.save(tmp_path / NAME))
    assert reloaded.active_volumes({}) == {"artifacts": "/var/www/html/artifacts"}


def test_declared_volume_must_be_an_absolute_container_path():
    with pytest.raises(ManifestError, match="absolute path"):
        Manifest.from_dict({"volumes": {"artifacts": "./data/artifacts"}})


def test_a_newer_manifest_version_is_refused(tmp_path):
    path = tmp_path / NAME
    path.write_text(yaml.safe_dump({"version": 99, "name": "x"}), encoding="utf-8")
    with pytest.raises(ManifestError, match="upgrade dockerbundle"):
        Manifest.load(path)


def test_invalid_env_rule_is_rejected():
    with pytest.raises(ManifestError, match="env.X"):
        Manifest.from_dict({"env": {"X": "whatever"}})


def test_invalid_mount_mode_is_rejected():
    with pytest.raises(ManifestError, match="expected copy|volume|skip"):
        Manifest.from_dict({"services": {"a": {"mounts": {"/x": "bake"}}}})


def test_replicas_must_be_positive():
    with pytest.raises(ManifestError, match="positive integer"):
        Manifest.from_dict({"services": {"a": {"replicas": 0}}})


def test_variant_without_a_base_is_rejected():
    with pytest.raises(ManifestError, match="no entry in base"):
        Manifest.from_dict({"variants": ["gpu"]})


def test_bad_port_range_is_rejected():
    with pytest.raises(ManifestError, match="port_range"):
        Manifest.from_dict({"port_range": [30000, 100]})


def test_unknown_source_type_is_rejected():
    with pytest.raises(ManifestError, match="unknown source type"):
        Manifest.from_dict({"sources": [{"type": "ftp", "path": "x"}]})


def test_a_bare_string_source_means_a_catalogue():
    manifest = Manifest.from_dict({"sources": ["../packages"]})
    assert manifest.sources[0].type == "catalog"


def test_output_dir_is_relative_to_the_manifest(tmp_path):
    manifest = Manifest(output="build/out")
    manifest.path = tmp_path / NAME
    assert manifest.output_dir() == (tmp_path / "build" / "out").resolve()


# -- mode -------------------------------------------------------------------


def test_every_mode_round_trips(tmp_path):
    raw = {
        "services": {
            "a": {"mode": "bake"},
            "b": {"mode": "sidecar"},
            "c": {"mode": "external"},
            "d": {"mode": False},  # `mode: off` is a YAML boolean
        }
    }
    manifest = Manifest.from_dict(raw)
    assert manifest.services["a"].mode is ServiceMode.BAKE
    assert manifest.services["b"].mode is ServiceMode.SIDECAR
    assert manifest.services["c"].mode is ServiceMode.EXTERNAL
    assert manifest.services["d"].mode is ServiceMode.OFF

    reloaded = Manifest.load(manifest.save(tmp_path / NAME))
    assert reloaded.services["d"].mode is ServiceMode.OFF


def test_mode_on_is_refused_because_it_names_nothing():
    with pytest.raises(ManifestError, match="not a mode"):
        Manifest.from_dict({"services": {"a": {"mode": True}}})


def test_unset_mode_follows_the_recipe():
    manifest = Manifest.from_dict({"services": {"a": {}}})
    assert manifest.service_mode("a", {}, default=ServiceMode.SIDECAR) is ServiceMode.SIDECAR
    assert manifest.service_mode("a", {}, default=ServiceMode.BAKE) is ServiceMode.BAKE


def test_a_service_no_entry_mentions_still_follows_the_recipe():
    manifest = Manifest()
    assert manifest.service_mode("ghost", {}, default=ServiceMode.BAKE) is ServiceMode.BAKE


# -- retired keys -----------------------------------------------------------


def test_env_conflicts_names_its_replacement():
    with pytest.raises(ManifestError, match="replaced by env:"):
        Manifest.from_dict({"env_conflicts": {"X": "prefix"}})


@pytest.mark.parametrize("key", ["enabled", "bakeable"])
def test_enabled_and_bakeable_name_their_replacement(key):
    with pytest.raises(ManifestError, match="replaced by mode:"):
        Manifest.from_dict({"services": {"a": {key: False}}})


# -- env rules --------------------------------------------------------------


def test_per_service_names_round_trip(tmp_path):
    manifest = Manifest.from_dict(
        {
            "env": {
                "TRAEFIK_DOMAIN": {
                    "per_service": {"app_nginx": "TRAEFIK_DOMAIN_APP", "vnu": "TRAEFIK_DOMAIN_VNU"}
                }
            }
        }
    )
    rule = manifest.env["TRAEFIK_DOMAIN"]
    assert rule.per_service == {
        "app_nginx": "TRAEFIK_DOMAIN_APP",
        "vnu": "TRAEFIK_DOMAIN_VNU",
    }

    reloaded = Manifest.load(manifest.save(tmp_path / NAME))
    assert reloaded.env["TRAEFIK_DOMAIN"].per_service == rule.per_service


def test_two_services_cannot_take_the_same_outer_name():
    with pytest.raises(ManifestError, match="both map to"):
        Manifest.from_dict({"env": {"D": {"per_service": {"a": "D_ONE", "b": "D_ONE"}}}})


def test_local_rule_needs_a_slug():
    with pytest.raises(ManifestError, match="needs a service slug"):
        Manifest.from_dict({"env": {"URL": "local:"}})


def test_unknown_key_in_an_env_rule_is_rejected():
    with pytest.raises(ManifestError, match="unknown key"):
        Manifest.from_dict({"env": {"X": {"rule": "prefix", "prefer": "a"}}})


# -- features ---------------------------------------------------------------


def test_features_gate_sources_services_volumes_and_env():
    manifest = Manifest.from_dict(
        {
            "features": {"mysql": True, "gpu": False},
            "sources": [
                {"type": "catalog", "path": "a", "when": "mysql"},
                {"type": "catalog", "path": "b", "when": "gpu"},
            ],
            "services": {"db": {"when": "mysql"}, "cuda": {"when": "gpu"}},
            "volumes": {"models": {"path": "/models", "when": "gpu"}},
            "env": {"DB_HOST": {"rule": "keep:db", "when": "mysql"}},
        }
    )
    features = manifest.features

    assert [s.path for s in manifest.active_sources(features)] == ["a"]
    assert manifest.service_mode("db", features, default=ServiceMode.BAKE) is ServiceMode.BAKE
    assert manifest.service_mode("cuda", features, default=ServiceMode.BAKE) is ServiceMode.OFF
    assert manifest.active_volumes(features) == {}
    assert set(manifest.active_env(features)) == {"DB_HOST"}


def test_a_negated_when_reads_the_other_way():
    manifest = Manifest.from_dict(
        {"features": {"mysql": True}, "services": {"external_db": {"when": "!mysql"}}}
    )
    mode = manifest.service_mode("external_db", {"mysql": True}, default=ServiceMode.BAKE)
    assert mode is ServiceMode.OFF


def test_a_when_naming_an_undeclared_feature_is_refused():
    with pytest.raises(ManifestError, match="not declared under features"):
        Manifest.from_dict({"services": {"db": {"when": "mysql"}}})


def test_features_must_be_booleans():
    with pytest.raises(ManifestError, match="true or false"):
        Manifest.from_dict({"features": {"mysql": "yes please"}})


def test_features_round_trip(tmp_path):
    manifest = Manifest.from_dict({"features": {"gpu": False, "mysql": True}})
    reloaded = Manifest.load(manifest.save(tmp_path / NAME))
    assert reloaded.features == {"gpu": False, "mysql": True}


def test_publish_accepts_a_spec_or_none(tmp_path):
    path = tmp_path / NAME
    path.write_text(
        "version: 2\nname: x\nservices:\n"
        "  php: {publish: {9000: none}}\n"
        "  web: {publish: {80: '127.0.0.1:8081'}}\n"
        "  quiet: {publish: {80: no}}\n",
        encoding="utf-8",
    )
    manifest = Manifest.load(path)
    assert manifest.services["php"].publish == {9000: "none"}
    assert manifest.services["web"].publish == {80: "127.0.0.1:8081"}
    # YAML reads a bare `no` as false; it means what `none` says.
    assert manifest.services["quiet"].publish == {80: "none"}


def test_publish_must_name_ports():
    with pytest.raises(ManifestError, match="not a port"):
        ServiceEntry.from_dict("php", {"publish": {"http": "none"}})


# -- binds ------------------------------------------------------------------


def test_binds_round_trip_with_the_default_host(tmp_path):
    manifest = Manifest.from_dict(
        {"binds": {"config": {"path": "/var/www/html/config", "ship": "app/config"}}}
    )
    reloaded = Manifest.load(manifest.save(tmp_path / NAME))
    bind = reloaded.active_binds({})["config"]

    assert (bind.path, bind.host, bind.ship) == ("/var/www/html/config", "./config", "app/config")
    # The default host is not written back: the file stays as short as it was.
    assert "host" not in yaml.safe_load((tmp_path / NAME).read_text())["binds"]["config"]


def test_a_bare_string_bind_is_its_container_path():
    bind = Manifest.from_dict({"binds": {"hosts": "/etc/app/hosts"}}).binds["hosts"]
    assert (bind.path, bind.host) == ("/etc/app/hosts", "./hosts")


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"path": "relative/config"}, "absolute"),
        ({"path": "/config", "host": "/srv/config"}, "inside the deployment"),
        ({"path": "/config", "host": "../config"}, "inside the deployment"),
        ({"path": "/config", "host": "./"}, "inside the deployment"),
        ({"path": "/config", "host": "./context/config"}, "generator writes"),
        ({"path": "/config", "copy": "x"}, "unknown key"),
    ],
)
def test_bad_binds_are_refused(entry, message):
    with pytest.raises(ManifestError, match=message):
        Manifest.from_dict({"binds": {"config": entry}})


def test_a_bind_name_must_make_an_environment_variable():
    with pytest.raises(ManifestError, match="environment variable"):
        Manifest.from_dict({"binds": {"My-Config": "/config"}})


def test_a_bind_over_a_volume_is_refused():
    # Docker would let the later mount win, and the volume's data would silently vanish.
    with pytest.raises(ManifestError, match="overlaps"):
        Manifest.from_dict(
            {
                "volumes": {"db": "/var/www/html/storage"},
                "binds": {"cfg": "/var/www/html/storage/cfg"},
            }
        )


def test_two_binds_cannot_share_a_host_path():
    with pytest.raises(ManifestError, match="already binds"):
        Manifest.from_dict(
            {"binds": {"a": {"path": "/a", "host": "./x"}, "b": {"path": "/b", "host": "./x"}}}
        )


def test_features_gate_binds():
    manifest = Manifest.from_dict(
        {"features": {"edit": False}, "binds": {"config": {"path": "/config", "when": "edit"}}}
    )
    assert manifest.active_binds({"edit": False}) == {}
    assert set(manifest.active_binds({"edit": True})) == {"config"}
