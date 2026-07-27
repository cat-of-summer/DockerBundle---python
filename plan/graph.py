"""Turn ``depends_on`` into supervisord start order.

supervisord has no notion of dependencies; it has ``priority``, a plain integer that
decides start order. Recipes already carry a sensible base priority per runtime
(databases 10, application servers 30, web servers 40), which encodes the ordering that
holds across almost every stack. The dependency graph refines it: a service is nudged
after everything it declares a dependency on.

Ordering alone does not mean *ready*, so the planner also emits ``wait_for`` probes into
the entrypoint. This module only decides sequence.
"""

from __future__ import annotations

from dataclasses import dataclass


class CycleError(ValueError):
    """``depends_on`` contains a cycle, so no start order exists."""


@dataclass
class Order:
    depth: dict[str, int]
    """Longest dependency chain leading to each service."""

    sequence: list[str]
    """Slugs in a valid start order."""


def topological(edges: dict[str, list[str]]) -> Order:
    """Order services so that every dependency precedes its dependants.

    ``edges`` maps a slug to the slugs it depends on. References to unknown slugs are
    ignored: a service may depend on something the user chose not to include.
    """
    nodes = list(edges)
    known = set(nodes)
    deps = {node: [d for d in edges[node] if d in known and d != node] for node in nodes}

    depth: dict[str, int] = {}
    sequence: list[str] = []
    # 0 = unvisited, 1 = on the current path, 2 = finished.
    state: dict[str, int] = dict.fromkeys(nodes, 0)

    def visit(node: str, trail: list[str]) -> int:
        marker = state[node]
        if marker == 1:
            cycle = [*trail[trail.index(node) :], node]
            raise CycleError("depends_on cycle: " + " -> ".join(cycle))
        if marker == 2:
            return depth[node]

        state[node] = 1
        trail.append(node)
        current = 0
        for dependency in deps[node]:
            current = max(current, visit(dependency, trail) + 1)
        trail.pop()
        state[node] = 2

        depth[node] = current
        sequence.append(node)
        return current

    for node in nodes:
        visit(node, [])

    return Order(depth=depth, sequence=sequence)


def priorities(
    edges: dict[str, list[str]], base: dict[str, int], *, step: int = 1
) -> dict[str, int]:
    """Combine recipe base priorities with dependency depth.

    The base value dominates so that runtime classes stay grouped; depth only breaks ties
    within a class and guarantees a dependant never starts before its dependency.
    """
    order = topological(edges)
    result = {slug: base.get(slug, 50) + order.depth.get(slug, 0) * step for slug in edges}

    # Enforce the invariant directly: a nudge based on depth is not enough when a
    # dependant's runtime class has a lower base priority than its dependency's.
    for slug in order.sequence:
        for dependency in edges.get(slug, []):
            if dependency in result and result[slug] <= result[dependency]:
                result[slug] = result[dependency] + step

    return result
