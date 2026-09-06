"""The generate pipeline, shared by the CLI and the wizard.

Keeping it here means ``dockerbundle generate --yes`` in CI and the interactive wizard
run exactly the same code, so what you preview is what CI produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.manifest import Manifest, ServiceEntry
from core.model import ServiceSpec
from discover import resolve
from plan import builder
from plan.builder import PlanError
from recipes.match import Registry
from render import writer


@dataclass
class Context:
    """Everything discovered and resolved for a configuration, before planning."""

    manifest: Manifest
    registry: Registry
    discovery: resolve.Discovery
    features: dict[str, bool] = field(default_factory=dict)
    selected: list[ServiceSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load(
    manifest: Manifest, *, pull: bool = False, features: dict[str, bool] | None = None
) -> Context:
    """Discover candidates for a configuration and work out which ones it selects.

    Every service a source provides is a candidate. What happens to each of them is one
    decision, ``mode:``, applied later by the planner — so ``services:`` stays a table of
    decisions rather than a second copy of the compose files, and adding an entry to
    force one service's recipe no longer quietly excludes all the others.
    """
    root = manifest.path.parent if manifest.path else Path.cwd()
    active = dict(features if features is not None else manifest.features)

    registry = Registry.load(manifest)
    discovery = resolve.collect(manifest.active_sources(active), root, pull=pull)

    warnings = [*registry.warnings, *discovery.warnings]
    warnings.extend(resolve.enrich(discovery.services, pull=pull))

    known = {spec.slug for spec in discovery.services}
    warnings.extend(
        f"{slug}: named in services: but no source provides it"
        for slug in sorted(set(manifest.services) - known)
    )

    return Context(
        manifest=manifest,
        registry=registry,
        discovery=discovery,
        features=active,
        selected=list(discovery.services),
        warnings=warnings,
    )


def ensure_entries(context: Context) -> None:
    """Add a configuration entry for every selected service that lacks one."""
    for spec in context.selected:
        context.manifest.services.setdefault(
            spec.slug,
            ServiceEntry(slug=spec.slug, package=spec.package, service=spec.name),
        )


def generate(
    context: Context, *, variant: str = "cpu", image_ref: str = "", output: Path | None = None
) -> writer.Written:
    """Plan and render. Raises :class:`PlanError` when something blocks generation."""
    if context.discovery.errors:
        raise PlanError(list(context.discovery.errors))

    plan = builder.build(
        context.selected,
        context.manifest,
        context.registry,
        variant=variant,
        features=context.features,
    )
    plan.warnings = [*context.warnings, *plan.warnings]
    destination = output or context.manifest.output_dir()
    # `--image` wins, then the configuration. Without that fallback the published
    # reference would live only in a flag, and any plain `generate` would quietly put
    # `<name>:latest` back into the compose file shipped to users.
    return writer.render(
        plan,
        destination,
        variant=variant,
        image_ref=image_ref or context.manifest.image,
        labels=context.manifest.active_labels(context.features),
    )


def conflicts(context: Context) -> list:
    """Environment conflicts that still need a decision.

    Exposed separately so the wizard can present them before generation is attempted,
    rather than making the user read them out of an exception.
    """
    from plan import envmerge
    from plan.builder import compute_prefixes

    prefixes = compute_prefixes(
        context.selected,
        {
            slug: entry.env_prefix
            for slug, entry in context.manifest.services.items()
            if entry.env_prefix
        },
    )
    merged = envmerge.merge(
        context.selected,
        globals_=context.manifest.globals,
        rules=context.manifest.active_env(context.features),
        prefixes=prefixes,
    )
    return merged.conflicts


__all__ = ["Context", "PlanError", "conflicts", "ensure_entries", "generate", "load"]
