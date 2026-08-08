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
    """Everything discovered and resolved for a manifest, before planning."""

    manifest: Manifest
    registry: Registry
    discovery: resolve.Discovery
    selected: list[ServiceSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load(manifest: Manifest, *, pull: bool = False) -> Context:
    """Discover candidates for a manifest and work out which ones it selects."""
    root = manifest.path.parent if manifest.path else Path.cwd()
    registry = Registry.load(root)
    discovery = resolve.collect(manifest.sources, root, pull=pull)

    warnings = [*registry.warnings, *discovery.warnings]
    warnings.extend(resolve.enrich(discovery.services, pull=pull))

    # An empty services table means "everything found", which is what a fresh `init`
    # produces; once the wizard has run, the manifest lists exactly what to include.
    if manifest.services:
        wanted = {slug for slug, entry in manifest.services.items() if entry.enabled}
        selected = [spec for spec in discovery.services if spec.slug in wanted]
        missing = wanted - {spec.slug for spec in selected}
        warnings.extend(
            f"{slug}: listed in bundle.yml but no source provides it" for slug in sorted(missing)
        )
    else:
        selected = list(discovery.services)

    return Context(
        manifest=manifest,
        registry=registry,
        discovery=discovery,
        selected=selected,
        warnings=warnings,
    )


def ensure_entries(context: Context) -> None:
    """Add a manifest entry for every selected service that lacks one."""
    for spec in context.selected:
        context.manifest.services.setdefault(
            spec.slug,
            ServiceEntry(slug=spec.slug, package=spec.package, service=spec.name),
        )


def generate(
    context: Context, *, variant: str = "cpu", image_ref: str = "", output: Path | None = None
) -> writer.Written:
    """Plan and render. Raises :class:`PlanError` when something blocks generation."""
    plan = builder.build(context.selected, context.manifest, context.registry, variant=variant)
    plan.warnings = [*context.warnings, *plan.warnings]
    destination = output or context.manifest.output_dir()
    # `--image` wins, then the manifest. Without the manifest fallback the published
    # reference would live only in a flag, and any plain `generate` would quietly put
    # `<name>:latest` back into the compose file shipped to users.
    return writer.render(
        plan,
        destination,
        variant=variant,
        image_ref=image_ref or context.manifest.image,
        labels=context.manifest.labels,
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
        decisions=context.manifest.env_conflicts,
        prefixes=prefixes,
    )
    return merged.conflicts


__all__ = ["Context", "PlanError", "conflicts", "ensure_entries", "generate", "load"]
