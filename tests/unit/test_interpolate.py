from __future__ import annotations

import pytest

from discover.interpolate import (
    InterpolationError,
    interpolate,
    interpolate_tree,
    referenced_names,
)


def test_plain_and_braced():
    values = {"MYSQL_VERSION": "8.0", "INSTANCE": "2"}
    assert interpolate("mysql:${MYSQL_VERSION}", values) == "mysql:8.0"
    assert interpolate("mysql$INSTANCE", values) == "mysql2"


def test_default_applies_when_unset_or_empty():
    # `:-` treats an empty value as absent; `-` only reacts to a missing key.
    assert interpolate("${PORT:-4096}", {}) == "4096"
    assert interpolate("${PORT:-4096}", {"PORT": ""}) == "4096"
    assert interpolate("${PORT-4096}", {"PORT": ""}) == ""
    assert interpolate("${PORT-4096}", {}) == "4096"


def test_missing_variable_is_empty_not_an_error():
    # A compose file is routinely read without the .env it was written against.
    assert interpolate("host=${NOPE}", {}) == "host="


def test_required_marker_raises():
    with pytest.raises(InterpolationError, match="must be set"):
        interpolate("${TOKEN:?must be set}", {})


def test_double_dollar_is_a_literal():
    # Compose escaping: entrypoints are full of `exec "$$@"`.
    assert interpolate('exec "$$@"', {}) == 'exec "$@"'


def test_tree_walks_keys_and_values():
    tree = {"image": "redis:${TAG}", "ports": ["${PORT}:6379"], "n": 5}
    result = interpolate_tree(tree, {"TAG": "7", "PORT": "6380"})
    assert result == {"image": "redis:7", "ports": ["6380:6379"], "n": 5}


def test_referenced_names():
    assert referenced_names("${A}-$B-${C:-x}") == {"A", "B", "C"}
