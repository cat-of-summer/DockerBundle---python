from __future__ import annotations

import pytest

from plan import graph


def test_topological_depth_and_order():
    order = graph.topological({"nginx": ["php"], "php": ["mysql"], "mysql": []})
    assert order.depth == {"mysql": 0, "php": 1, "nginx": 2}
    assert order.sequence.index("mysql") < order.sequence.index("php")
    assert order.sequence.index("php") < order.sequence.index("nginx")


def test_cycle_is_reported_with_the_path():
    with pytest.raises(graph.CycleError, match="a -> b -> a"):
        graph.topological({"a": ["b"], "b": ["a"]})


def test_unknown_dependencies_are_ignored():
    # A service may depend on something the user chose not to include.
    order = graph.topological({"nginx": ["absent"]})
    assert order.depth == {"nginx": 0}


def test_priorities_keep_runtime_classes_grouped():
    base = {"mysql": 10, "php": 30, "nginx": 40}
    result = graph.priorities({"nginx": ["php"], "php": ["mysql"], "mysql": []}, base)
    assert result["mysql"] < result["php"] < result["nginx"]
    assert result["mysql"] == 10


def test_dependant_is_pushed_past_a_higher_priority_dependency():
    # The base values alone would start nginx before the queue worker it depends on.
    base = {"queue": 60, "nginx": 40}
    result = graph.priorities({"nginx": ["queue"], "queue": []}, base)
    assert result["nginx"] > result["queue"]
