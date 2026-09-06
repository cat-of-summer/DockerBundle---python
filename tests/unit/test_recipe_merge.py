"""Layering, ``extends:`` and feature-gated rules.

The behaviour these cover is the reason a project no longer has to vendor a whole
built-in recipe to change one line of it.
"""

from __future__ import annotations

import pytest

from core.manifest import Manifest
from recipes import schema
from recipes.match import Registry

CONFIG = "docker-bundle.yml"


def raw(name: str, **body) -> schema.RawRecipe:
    return schema.RawRecipe(name=name, body={"name": name, **body}, origin=f"<{name}>")


def child(name: str, parent: str, **body) -> schema.RawRecipe:
    return schema.RawRecipe(
        name=name, body={"name": name, "extends": parent, **body}, origin=f"<{name}>"
    )


def build(*layers: list[schema.RawRecipe]) -> dict[str, schema.Recipe]:
    table, warnings = schema.build_table(list(layers))
    assert not warnings, warnings
    return table


# -- replace vs extend ------------------------------------------------------


def test_a_same_named_recipe_without_extends_replaces_outright():
    base = raw("nginx", install={"debian": ["nginx-light"]}, shared="nginx")
    override = raw("nginx", install={"debian": ["nginx-full"]})
    table = build([base], [override])

    assert table["nginx"].install == {"debian": ["nginx-full"]}
    # Nothing of the original survives: that is what replacing means, and it stays
    # available for the cases where it is what you want.
    assert table["nginx"].shared == ""


def test_extends_inherits_what_the_child_does_not_mention():
    base = raw("nginx", shared="nginx", priority=60, install={"debian": ["nginx-light"]})
    table = build([base], [child("nginx", "nginx", families=["debian"])])

    assert table["nginx"].shared == "nginx"
    assert table["nginx"].priority == 60
    assert table["nginx"].families == ["debian"]


def test_plus_appends_and_a_plain_key_replaces():
    base = raw(
        "nginx",
        install={"debian": ["nginx-light", "openssl"]},
        post_copy=["sed -i one"],
        copy=[{"src": "nginx.conf", "dest": "/etc/nginx/a.conf"}],
    )
    tuned = child(
        "nginx",
        "nginx",
        **{
            "+install": {"debian": ["iproute2"]},
            "+post_copy": ["sed -i two"],
            "copy": [{"src": "configs", "dest": "/etc/nginx/configs"}],
        },
    )
    recipe = build([base], [tuned])["nginx"]

    assert recipe.install == {"debian": ["nginx-light", "openssl", "iproute2"]}
    assert [step.cmd for step in recipe.post_copy] == ["sed -i one", "sed -i two"]
    assert [rule.dest for rule in recipe.copy] == ["/etc/nginx/configs"]


def test_an_empty_list_clears_what_the_parent_declared():
    base = raw("nginx", pre_init=["cp -f a b"], post_init=["mv c d"])
    recipe = build([base], [child("nginx", "nginx", pre_init=[], post_init=[])])["nginx"]

    assert recipe.pre_init == []
    assert recipe.post_init == []


def test_plus_merges_mapping_leaves_by_key():
    base = raw("nginx", mount_kinds={"/etc/nginx/*": "config", "/var/www/*": "code"})
    tuned = child("nginx", "nginx", **{"+mount_kinds": {"/var/www/*": "skip"}})
    recipe = build([base], [tuned])["nginx"]

    assert recipe.mount_kinds == {"/etc/nginx/*": "config", "/var/www/*": "skip"}


def test_extends_reaches_the_same_layer_whatever_order_the_files_were_read_in():
    tuned = child("nginx-tuned", "nginx-base", priority=70)
    base = raw("nginx-base", shared="nginx")
    table = build([tuned, base])

    assert table["nginx-tuned"].shared == "nginx"
    assert table["nginx-tuned"].priority == 70


def test_extends_naming_nothing_is_an_error():
    with pytest.raises(schema.RecipeError, match="not defined in any earlier layer"):
        schema.build_table([[child("x", "ghost")]])


def test_a_cycle_between_two_recipes_is_an_error():
    with pytest.raises(schema.RecipeError, match="cycle"):
        schema.build_table([[child("a", "b"), child("b", "a")]])


def test_plus_without_extends_is_refused():
    with pytest.raises(schema.RecipeError, match="only mean something with extends"):
        schema.from_dict({"name": "x", "+install": {"debian": ["a"]}})


def test_plus_cannot_append_a_string_to_a_list():
    with pytest.raises(schema.RecipeError, match="cannot append"):
        schema.build_table([[raw("x", install=["a"])], [child("x", "x", **{"+install": "nope"})]])


# -- inline recipes ---------------------------------------------------------


def test_inline_recipes_take_their_name_from_the_key():
    entries, warnings = schema.read_inline({"vnu": {"priority": 60}}, CONFIG)
    assert not warnings
    assert entries[0].name == "vnu"
    assert entries[0].body["name"] == "vnu"


def test_an_inline_body_that_names_itself_differently_warns():
    entries, warnings = schema.read_inline({"vnu": {"name": "other"}}, CONFIG)
    assert entries[0].name == "vnu"
    assert any("other" in warning for warning in warnings)


def test_an_inline_recipe_can_extend_a_builtin(registry: Registry, tmp_path):
    manifest = Manifest(
        name="stand",
        recipes={"nginx": {"extends": "nginx", "+install": {"debian": ["iproute2"]}}},
        path=tmp_path / CONFIG,
    )
    loaded = Registry.load(manifest)

    assert not loaded.warnings
    builtin = registry.recipes["nginx"].install["debian"]
    assert loaded.recipes["nginx"].install["debian"] == [*builtin, "iproute2"]
    # Everything the built-in set up and the child never mentioned is still there —
    # which is the whole point of not having to copy it.
    assert loaded.recipes["nginx"].shared == "nginx"
    assert loaded.recipes["nginx"].port.mechanism == "nginx_conf"


def test_recipe_paths_are_read_as_a_layer(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "vnu.yml").write_text(
        "name: vnu\npriority: 60\nsupervisor:\n  - {name: vnu, command: java -jar vnu}\n",
        encoding="utf-8",
    )
    manifest = Manifest(name="stand", recipe_paths=["shared"], path=tmp_path / CONFIG)
    loaded = Registry.load(manifest)

    assert "vnu" in loaded.recipes
    assert loaded.recipes["vnu"].programs[0].command == "java -jar vnu"


def test_an_inline_recipe_can_extend_one_from_recipe_paths(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "vnu.yml").write_text("name: vnu\npriority: 60\nshared: java\n", encoding="utf-8")
    manifest = Manifest(
        name="stand",
        recipe_paths=["shared"],
        recipes={"vnu": {"extends": "vnu", "priority": 80}},
        path=tmp_path / CONFIG,
    )
    loaded = Registry.load(manifest)

    assert loaded.recipes["vnu"].priority == 80
    assert loaded.recipes["vnu"].shared == "java"


def test_a_missing_recipe_path_warns_rather_than_stopping(tmp_path):
    manifest = Manifest(name="stand", recipe_paths=["nowhere"], path=tmp_path / CONFIG)
    loaded = Registry.load(manifest)

    assert any("nowhere" in warning for warning in loaded.warnings)
    assert "nginx" in loaded.recipes


# -- when: on recipe rules --------------------------------------------------


def test_rules_held_behind_a_feature_drop_out_when_it_is_off():
    recipe = schema.from_dict(
        {
            "name": "x",
            "post_copy": ["always", {"cmd": "only with gpu", "when": "gpu"}],
            "copy": [
                {"src": "a", "dest": "/a"},
                {"src": "b", "dest": "/b", "when": "gpu"},
            ],
            "supervisor": [
                {"name": "p", "command": "run"},
                {"name": "g", "command": "run --gpu", "when": "gpu"},
            ],
            "readiness": [{"type": "tcp", "target": "{port}", "when": "gpu"}],
        }
    )

    off = {"gpu": False}
    assert recipe.post_copy_for(off) == ["always"]
    assert [rule.dest for rule in recipe.copies_for(off)] == ["/a"]
    assert [rule.name for rule in recipe.programs_for(off)] == ["p"]
    assert recipe.readiness_for(off) == []

    on = {"gpu": True}
    assert recipe.post_copy_for(on) == ["always", "only with gpu"]
    assert [rule.name for rule in recipe.programs_for(on)] == ["p", "g"]
    assert len(recipe.readiness_for(on)) == 1


def test_run_steps_can_be_held_behind_a_feature():
    recipe = schema.from_dict(
        {
            "name": "x",
            "run": {"debian": ["apt-get update", {"cmd": "install cuda", "when": "gpu"}]},
        }
    )
    assert recipe.runs_for("debian", {"gpu": False}) == ["apt-get update"]
    assert recipe.runs_for("debian", {"gpu": True}) == ["apt-get update", "install cuda"]


def test_a_step_naming_an_unknown_feature_is_an_error():
    recipe = schema.from_dict({"name": "x", "post_copy": [{"cmd": "c", "when": "gpu"}]})
    with pytest.raises(schema.RecipeError, match="unknown feature"):
        recipe.post_copy_for({})


def test_a_step_mapping_needs_a_cmd():
    with pytest.raises(schema.RecipeError, match="needs a cmd"):
        schema.from_dict({"name": "x", "post_copy": [{"when": "gpu"}]})


# -- extra match signals ----------------------------------------------------


def test_path_and_env_can_claim_a_service_nothing_else_identifies():
    match = schema.RecipeMatch(path=["*/services/nginx"], env=["NGINX_CONF"])
    scored = match.score(
        image="",
        files=set(),
        command="",
        service="web",
        path="/stand/services/nginx",
        env={"NGINX_CONF"},
    )
    assert scored == 15
    assert match.score(image="", files=set(), command="", service="web") == 0
