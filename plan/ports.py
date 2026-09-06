"""Give every baked service a port of its own.

Inside one container all services share a network namespace, so the two ``nginx``
services from ``laravel-nginx`` and ``vue-nginx-vite`` would both try to bind ``:80``.
The first keeps the port; later claimants are moved into a private range and their
configuration is rewritten through the mechanism their recipe declares.

A service whose recipe offers no mechanism (``none`` — notably anything rootfs-imported)
cannot be moved. When such a service loses a contest the conflict is reported rather
than papered over, because generating an image that cannot start is the worse outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.model import PortSpec, ServiceSpec
from recipes.schema import Recipe


@dataclass
class Allocation:
    """The outcome of assigning ports across the whole bundle."""

    ports: dict[str, list[PortSpec]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def for_service(self, slug: str) -> list[PortSpec]:
        return self.ports.get(slug, [])

    def primary(self, slug: str) -> int | None:
        entries = self.ports.get(slug) or []
        return entries[0].container if entries else None


def _desired(spec: ServiceSpec, recipe: Recipe) -> list[PortSpec]:
    """The ports a service would like, before any contention is resolved."""
    if spec.ports:
        return [
            PortSpec(
                container=p.original,
                original=p.original,
                published=p.published,
                protocol=p.protocol,
            )
            for p in spec.ports
        ]
    if recipe.port and recipe.port.default:
        return [PortSpec(container=recipe.port.default, original=recipe.port.default)]
    return []


def _can_move(recipe: Recipe) -> bool:
    return bool(recipe.port and recipe.port.mechanism != "none")


def allocate(
    services: list[tuple[ServiceSpec, Recipe]],
    *,
    pinned: dict[str, dict[int, int]] | None = None,
    port_range: tuple[int, int] = (20000, 20999),
) -> Allocation:
    """Assign a unique container port to every port of every service.

    ``pinned`` maps ``slug -> {original: assigned}`` from ``docker-bundle.yml``; pinned ports are
    honoured first so that a committed manifest keeps producing the same image.
    """
    pinned = pinned or {}
    result = Allocation()

    taken: dict[int, str] = {}
    low, high = port_range
    cursor = low

    def next_free() -> int | None:
        nonlocal cursor
        while cursor <= high:
            candidate = cursor
            cursor += 1
            if candidate not in taken:
                return candidate
        return None

    # Pinned assignments are reserved up front, before anyone competes for a default.
    for slug, mapping in pinned.items():
        for assigned in mapping.values():
            taken[assigned] = slug

    for spec, recipe in services:
        assigned_ports: list[PortSpec] = []
        service_pins = pinned.get(spec.slug, {})

        for want in _desired(spec, recipe):
            pin = service_pins.get(want.original)
            if pin is not None:
                want.container = pin
                assigned_ports.append(want)
                continue

            holder = taken.get(want.original)
            if holder is None:
                taken[want.original] = spec.slug
                assigned_ports.append(want)
                continue

            if not _can_move(recipe):
                result.errors.append(
                    f"{spec.slug}: port {want.original} is already used by {holder}, and its "
                    f"recipe cannot relocate it. Pin a free port in docker-bundle.yml "
                    f"(services.{spec.slug}.ports) or drop one of the two services."
                )
                assigned_ports.append(want)
                continue

            moved = next_free()
            if moved is None:
                result.errors.append(
                    f"{spec.slug}: no free port left in range {low}-{high}; widen port_range"
                )
                assigned_ports.append(want)
                continue

            taken[moved] = spec.slug
            want.container = moved
            assigned_ports.append(want)
            result.warnings.append(
                f"{spec.slug}: port {want.original} taken by {holder}; moved to {moved}"
            )

        result.ports[spec.slug] = assigned_ports

    return result
