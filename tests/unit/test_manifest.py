from __future__ import annotations

import pytest
import yaml

from core.manifest import Manifest, ManifestError, ServiceEntry, SourceRef


def test_round_trip_preserves_decisions(tmp_path):
    manifest = Manifest(
        name="shop",
        sources=[SourceRef(type="catalog", path="../packages")],
        env_conflicts={"EXTERNAL_ACCESS": "prefix"},
        services={
            "mysql": ServiceEntry(slug="mysql", ports={3306: 3307}, replicas=1),
            "queue": ServiceEntry(slug="queue", replicas=4, mounts={"/data": "volume"}),
        },
    )
    path = manifest.save(tmp_path / "bundle.yml")
    reloaded = Manifest.load(path)

    assert reloaded.name == "shop"
    assert reloaded.env_conflicts == {"EXTERNAL_ACCESS": "prefix"}
    assert reloaded.services["mysql"].ports == {3306: 3307}
    assert reloaded.services["queue"].replicas == 4
    assert reloaded.services["queue"].mounts == {"/data": "volume"}
    assert reloaded.sources[0].path == "../packages"


def test_image_reference_round_trips(tmp_path):
    # Kept in the manifest so a plain `generate` cannot revert the published reference.
    manifest = Manifest(name="stand", image="ghcr.io/acme/stand:latest")
    reloaded = Manifest.load(manifest.save(tmp_path / "bundle.yml"))
    assert reloaded.image == "ghcr.io/acme/stand:latest"


def test_forced_labels_round_trip(tmp_path):
    manifest = Manifest(name="stand", labels={"traefik.enable": "${TRAEFIK_ENABLE:-true}"})
    reloaded = Manifest.load(manifest.save(tmp_path / "bundle.yml"))
    assert reloaded.labels == {"traefik.enable": "${TRAEFIK_ENABLE:-true}"}


def test_declared_volumes_round_trip(tmp_path):
    manifest = Manifest(name="stand", volumes={"artifacts": "/var/www/html/artifacts"})
    reloaded = Manifest.load(manifest.save(tmp_path / "bundle.yml"))
    assert reloaded.volumes == {"artifacts": "/var/www/html/artifacts"}


def test_declared_volume_must_be_an_absolute_container_path():
    with pytest.raises(ManifestError, match="absolute path"):
        Manifest.from_dict({"volumes": {"artifacts": "./data/artifacts"}})


def test_a_newer_manifest_version_is_refused(tmp_path):
    path = tmp_path / "bundle.yml"
    path.write_text(yaml.safe_dump({"version": 99, "name": "x"}), encoding="utf-8")
    with pytest.raises(ManifestError, match="upgrade dockerbundle"):
        Manifest.load(path)


def test_invalid_env_conflict_rule_is_rejected():
    with pytest.raises(ManifestError, match="env_conflicts"):
        Manifest.from_dict({"env_conflicts": {"X": "whatever"}})


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
    manifest.path = tmp_path / "bundle.yml"
    assert manifest.output_dir() == (tmp_path / "build" / "out").resolve()
