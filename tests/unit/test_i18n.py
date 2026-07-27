from __future__ import annotations

import json

from core.paths import resource_dir
from ui import i18n


def catalog(code: str) -> dict[str, str]:
    return json.loads((resource_dir("lang") / f"{code}.json").read_text(encoding="utf-8-sig"))


def test_every_language_has_the_same_keys():
    reference = set(catalog("en"))
    for code in i18n.available_languages():
        if code == "en":
            continue
        other = set(catalog(code))
        assert not reference - other, f"{code} is missing: {sorted(reference - other)}"
        assert not other - reference, f"{code} has orphans: {sorted(other - reference)}"


def test_no_empty_translations():
    for code in i18n.available_languages():
        for key, value in catalog(code).items():
            assert value.strip(), f"{code}.{key} is empty"


def test_placeholders_match_between_languages():
    import re

    fields = re.compile(r"\{([a-z_]+)")
    for key, template in catalog("en").items():
        expected = set(fields.findall(template))
        for code in i18n.available_languages():
            if code == "en":
                continue
            actual = set(fields.findall(catalog(code).get(key, "")))
            assert actual == expected, f"{code}.{key}: {actual} != {expected}"


def test_lookup_falls_back_to_english_then_to_the_key():
    i18n.set_language("ru")
    assert i18n.t("cli.ok") != "cli.ok"
    assert i18n.t("no.such.key") == "no.such.key"


def test_formatting_survives_a_bad_placeholder():
    i18n.set_language("en")
    # A template that wants params but gets none must not raise.
    assert i18n.t("cli.generated") == i18n.t("cli.generated")
    assert "dist" in i18n.t("cli.generated", path="dist")


def test_normalise_accepts_locale_forms():
    assert i18n.normalise("ru_RU.UTF-8") == "ru"
    assert i18n.normalise("en-GB") == "en"
    assert i18n.normalise("de") == ""
    assert i18n.normalise(None) == ""


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("DOCKERBUNDLE_LANG", "ru")
    assert i18n.set_language("en") == "ru"
