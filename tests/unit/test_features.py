from __future__ import annotations

import pytest

from core import features


def test_a_bare_name_reads_the_flag():
    assert features.evaluate(features.normalise("mysql", where="w"), {"mysql": True})
    assert not features.evaluate(features.normalise("mysql", where="w"), {"mysql": False})


def test_a_bang_negates():
    terms = features.normalise("!mysql", where="w")
    assert features.evaluate(terms, {"mysql": False})
    assert not features.evaluate(terms, {"mysql": True})


def test_a_list_means_all_of_them():
    terms = features.normalise(["gpu", "!mysql"], where="w")
    assert features.evaluate(terms, {"gpu": True, "mysql": False})
    assert not features.evaluate(terms, {"gpu": True, "mysql": True})
    assert not features.evaluate(terms, {"gpu": False, "mysql": False})


def test_no_condition_always_holds():
    assert features.evaluate(features.normalise(None, where="w"), {})


def test_whitespace_around_a_negation_is_tolerated():
    assert features.normalise("! mysql", where="w") == ["!mysql"]


def test_an_unknown_flag_is_an_error_not_a_silent_false():
    # A typo would otherwise drop the service it guards without saying anything.
    with pytest.raises(features.FeatureError, match="unknown feature"):
        features.evaluate(features.normalise("mysqel", where="w"), {"mysql": True})


def test_when_true_is_refused_because_it_names_no_flag():
    with pytest.raises(features.FeatureError, match="feature name"):
        features.normalise(True, where="w")


def test_an_empty_term_is_refused():
    with pytest.raises(features.FeatureError, match="empty term"):
        features.normalise("!", where="w")


def test_names_reports_what_an_expression_reads():
    assert features.names(["gpu", "!mysql"]) == {"gpu", "mysql"}


# -- parsing and overrides --------------------------------------------------


def test_features_must_be_booleans():
    with pytest.raises(features.FeatureError, match="true or false"):
        features.parse_features({"mysql": 1})


def test_parse_accepts_an_absent_table():
    assert features.parse_features(None) == {}


def test_overrides_win_over_the_file():
    base = {"mysql": True, "gpu": False}
    assert features.apply_overrides(base, enable=["gpu"], disable=["mysql"]) == {
        "mysql": False,
        "gpu": True,
    }
    # The original is left alone: two variants generated in one run must not bleed.
    assert base == {"mysql": True, "gpu": False}


def test_overriding_a_flag_nobody_declared_is_an_error():
    with pytest.raises(features.FeatureError, match="no such feature"):
        features.apply_overrides({"mysql": True}, enable=["gpu"], disable=[])
    with pytest.raises(features.FeatureError, match="no such feature"):
        features.apply_overrides({"mysql": True}, enable=[], disable=["gpu"])
