from __future__ import annotations

from core.manifest import EnvRule
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
        rules={"DB_HOST": EnvRule(rule="prefix")},
        prefixes={"laravel": "LARAVEL", "api": "API"},
    )
    assert not merged.conflicts
    assert {e.key for e in merged.entries} == {"LARAVEL_DB_HOST", "API_DB_HOST"}
    assert merged.rename_for("laravel", "DB_HOST") == "LARAVEL_DB_HOST"


def test_keep_and_literal_resolutions():
    specs = [service("a", {"X": "1"}), service("b", {"X": "2"})]
    kept = envmerge.merge(specs, globals_=[], rules={"X": EnvRule(rule="keep:b")})
    assert [(e.key, e.value) for e in kept.entries] == [("X", "2")]

    literal = envmerge.merge(specs, globals_=[], rules={"X": EnvRule(rule="value:9")})
    assert [(e.key, e.value) for e in literal.entries] == [("X", "9")]


def test_process_environment_maps_renamed_keys_back():
    # The application still expects DB_HOST, whatever the merged .env calls it.
    laravel = service("laravel", {"DB_HOST": "mysql"}, raw={"DB_HOST": "${DB_HOST}"})
    merged = envmerge.merge(
        [laravel, service("api", {"DB_HOST": "postgres"})],
        globals_=[],
        rules={"DB_HOST": EnvRule(rule="prefix")},
        prefixes={"laravel": "LARAVEL", "api": "API"},
    )
    assert envmerge.process_environment(laravel, merged) == {"DB_HOST": "%(ENV_LARAVEL_DB_HOST)s"}


def test_process_environment_is_empty_without_renames():
    spec = service("solo", {"X": "1"})
    merged = envmerge.merge([spec], globals_=[])
    assert envmerge.process_environment(spec, merged) == {}


def test_explicit_literal_applies_even_without_a_conflict():
    # A decision written down must not be silently skipped just because the packages
    # happen to agree today.
    merged = envmerge.merge(
        [service("a", {"API": "http://localhost"}), service("b", {"API": "http://localhost"})],
        globals_=[],
        rules={"API": EnvRule(rule="value:/api")},
    )
    assert [(e.key, e.value) for e in merged.entries] == [("API", "/api")]


# -- per_service ------------------------------------------------------------


def test_per_service_uses_the_names_the_author_chose():
    nginx = service("app_nginx", {"DOMAIN": "app.localhost"}, raw={"DOMAIN": "${DOMAIN}"})
    vnu = service("vnu", {"DOMAIN": "vnu.localhost"})
    merged = envmerge.merge(
        [nginx, vnu],
        globals_=[],
        rules={
            "DOMAIN": EnvRule(per_service={"app_nginx": "DOMAIN_1", "vnu": "DOMAIN_2"})
        },
    )

    assert not merged.conflicts
    assert not merged.errors
    assert {(e.key, e.value) for e in merged.entries} == {
        ("DOMAIN_1", "app.localhost"),
        ("DOMAIN_2", "vnu.localhost"),
    }
    # Each process still sees the name it was written against.
    assert envmerge.process_environment(nginx, merged) == {"DOMAIN": "%(ENV_DOMAIN_1)s"}


def test_per_service_applies_even_when_the_values_agree():
    merged = envmerge.merge(
        [service("a", {"D": "same"}), service("b", {"D": "same"})],
        globals_=[],
        rules={"D": EnvRule(per_service={"a": "D_A", "b": "D_B"})},
    )
    assert {e.key for e in merged.entries} == {"D_A", "D_B"}


def test_a_service_left_out_of_per_service_keeps_the_plain_name():
    merged = envmerge.merge(
        [service("a", {"D": "one"}), service("b", {"D": "two"})],
        globals_=[],
        rules={"D": EnvRule(per_service={"a": "D_A"})},
    )
    assert {(e.key, e.value) for e in merged.entries} == {("D_A", "one"), ("D", "two")}


def test_services_left_out_of_per_service_still_conflict_with_each_other():
    merged = envmerge.merge(
        [service("a", {"D": "one"}), service("b", {"D": "two"}), service("c", {"D": "three"})],
        globals_=[],
        rules={"D": EnvRule(per_service={"a": "D_A"})},
    )
    assert [c.key for c in merged.conflicts] == ["D"]
    assert set(merged.conflicts[0].values) == {"b", "c"}


def test_per_service_naming_a_service_that_is_not_here_warns():
    merged = envmerge.merge(
        [service("a", {"D": "one"})],
        globals_=[],
        rules={"D": EnvRule(per_service={"a": "D_A", "ghost": "D_G"})},
    )
    assert any("ghost" in warning for warning in merged.warnings)


def test_two_values_landing_on_one_key_is_an_error():
    # The renamed key collides with a key another service already owns outright.
    merged = envmerge.merge(
        [service("a", {"D": "one"}), service("b", {"D": "two", "D_A": "taken"})],
        globals_=[],
        rules={"D": EnvRule(per_service={"a": "D_A"})},
    )
    assert any("D_A" in error for error in merged.errors)


# -- local: -----------------------------------------------------------------


def test_local_expands_to_the_port_the_service_was_actually_given():
    merged = envmerge.merge(
        [service("app", {"VNU_URL": "http://vnu:8888"}), service("vnu", {"VNU_URL": ""})],
        globals_=[],
        rules={"VNU_URL": EnvRule(rule="value:http://{local:vnu}")},
        ports={"vnu": 20001},
    )
    assert [(e.key, e.value) for e in merged.entries] == [("VNU_URL", "http://127.0.0.1:20001")]


def test_bare_local_rule_gives_host_and_port():
    merged = envmerge.merge(
        [service("app", {"DB": "mysql:3306"})],
        globals_=[],
        rules={"DB": EnvRule(rule="local:mysql")},
        ports={"mysql": 3306},
    )
    assert merged.entries[0].value == "127.0.0.1:3306"


def test_local_naming_a_service_outside_the_bundle_is_an_error():
    merged = envmerge.merge(
        [service("app", {"DB": "mysql:3306"})],
        globals_=[],
        rules={"DB": EnvRule(rule="local:mysql")},
        ports={},
    )
    assert any("local:mysql" in error for error in merged.errors)


def test_a_literal_rule_introduces_a_key_no_package_declares():
    # `DB_ADDR: local:mysql` exists exactly because nothing in the sources knows the
    # address a service ends up on inside the bundle.
    merged = envmerge.merge(
        [service("app", {"OTHER": "x"})],
        globals_=[],
        rules={"DB_ADDR": EnvRule(rule="local:mysql"), "FLAG": EnvRule(rule="value:on")},
        ports={"mysql": 3306},
    )
    values = {e.key: e.value for e in merged.entries}
    assert values["DB_ADDR"] == "127.0.0.1:3306"
    assert values["FLAG"] == "on"


def test_a_prefix_rule_for_an_undeclared_key_adds_nothing():
    # Nothing to rename means nothing to write; only literals introduce keys.
    merged = envmerge.merge(
        [service("app", {"OTHER": "x"})],
        globals_=[],
        rules={"GHOST": EnvRule(rule="prefix")},
    )
    assert [e.key for e in merged.entries] == ["OTHER"]
