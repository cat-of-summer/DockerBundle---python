from __future__ import annotations

from core.model import EnvVar, Origin, ServiceSpec
from plan import envmerge


def service(slug: str, env: dict[str, str], raw: dict[str, str] | None = None) -> ServiceSpec:
    return ServiceSpec(
        slug=slug,
        name=slug,
        package=slug,
        origin=Origin.CATALOG,
        env_vars=[EnvVar(key=k, value=v, source=slug) for k, v in env.items()],
        raw_environment=raw or {},
    )


def test_identical_values_are_shared_not_conflicting():
    merged = envmerge.merge(
        [service("a", {"NETWORK": "network"}), service("b", {"NETWORK": "network"})],
        globals_=[],
    )
    assert not merged.conflicts
    assert [e.key for e in merged.entries] == ["NETWORK"]


def test_differing_values_are_reported_rather_than_silently_dropped():
    # This is exactly what bundle.sh's `sort -u` lost.
    merged = envmerge.merge(
        [
            service("laravel", {"EXTERNAL_ACCESS": "127.0.0.1:8081:80"}),
            service("vue", {"EXTERNAL_ACCESS": "127.0.0.1:8082:80"}),
        ],
        globals_=[],
    )
    assert [c.key for c in merged.conflicts] == ["EXTERNAL_ACCESS"]
    assert merged.conflicts[0].values == {
        "laravel": "127.0.0.1:8081:80",
        "vue": "127.0.0.1:8082:80",
    }
    # Nothing is emitted for an undecided conflict; generation must stop first.
    assert merged.entries == []


def test_declared_globals_never_conflict():
    merged = envmerge.merge(
        [service("a", {"INSTANCE": ""}), service("b", {"INSTANCE": "2"})],
        globals_=["INSTANCE"],
    )
    assert not merged.conflicts
    # A non-empty value is preferred over a blank one.
    assert merged.entries[0].value == "2"
    assert merged.warnings


def test_prefix_resolution_renames_per_service():
    merged = envmerge.merge(
        [service("laravel", {"DB_HOST": "mysql"}), service("api", {"DB_HOST": "postgres"})],
        globals_=[],
        decisions={"DB_HOST": "prefix"},
        prefixes={"laravel": "LARAVEL", "api": "API"},
    )
    assert not merged.conflicts
    assert {e.key for e in merged.entries} == {"LARAVEL_DB_HOST", "API_DB_HOST"}
    assert merged.rename_for("laravel", "DB_HOST") == "LARAVEL_DB_HOST"


def test_keep_and_literal_resolutions():
    specs = [service("a", {"X": "1"}), service("b", {"X": "2"})]
    kept = envmerge.merge(specs, globals_=[], decisions={"X": "keep:b"})
    assert [(e.key, e.value) for e in kept.entries] == [("X", "2")]

    literal = envmerge.merge(specs, globals_=[], decisions={"X": "value:9"})
    assert [(e.key, e.value) for e in literal.entries] == [("X", "9")]


def test_process_environment_maps_renamed_keys_back():
    # The application still expects DB_HOST, whatever the merged .env calls it.
    laravel = service("laravel", {"DB_HOST": "mysql"}, raw={"DB_HOST": "${DB_HOST}"})
    merged = envmerge.merge(
        [laravel, service("api", {"DB_HOST": "postgres"})],
        globals_=[],
        decisions={"DB_HOST": "prefix"},
        prefixes={"laravel": "LARAVEL", "api": "API"},
    )
    assert envmerge.process_environment(laravel, merged) == {
        "DB_HOST": "%(ENV_LARAVEL_DB_HOST)s"
    }


def test_process_environment_is_empty_without_renames():
    spec = service("solo", {"X": "1"})
    merged = envmerge.merge([spec], globals_=[])
    assert envmerge.process_environment(spec, merged) == {}


def test_explicit_literal_applies_even_without_a_conflict():
    # A decision written in bundle.yml must not be silently skipped just because the
    # packages happen to agree today.
    merged = envmerge.merge(
        [service("a", {"API": "http://localhost"}), service("b", {"API": "http://localhost"})],
        globals_=[],
        decisions={"API": "value:/api"},
    )
    assert [(e.key, e.value) for e in merged.entries] == [("API", "/api")]
