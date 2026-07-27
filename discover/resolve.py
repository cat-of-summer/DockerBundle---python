"""Turn the manifest's ``sources:`` list into a flat set of candidate services."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.manifest import SourceRef
from core.model import Origin, ServiceSpec
from discover import catalog, composefile, dockerps, imageref


@dataclass
class Discovery:
    services: list[ServiceSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def by_slug(self) -> dict[str, ServiceSpec]:
        return {spec.slug: spec for spec in self.services}


def _dedupe(specs: list[ServiceSpec], warnings: list[str]) -> list[ServiceSpec]:
    """Keep the first service claiming each slug and warn about the rest.

    Two sources legitimately describe the same thing — a catalogue folder and the
    container running from it — so a collision is expected, not fatal.
    """
    seen: dict[str, ServiceSpec] = {}
    unique: list[ServiceSpec] = []
    for spec in specs:
        previous = seen.get(spec.slug)
        if previous is not None:
            warnings.append(
                f"slug {spec.slug!r} claimed by both {previous.origin.value} "
                f"({previous.package}) and {spec.origin.value} ({spec.package}); "
                f"keeping the {previous.origin.value} one"
            )
            continue
        seen[spec.slug] = spec
        unique.append(spec)
    return unique


def collect(sources: list[SourceRef], root: Path, *, pull: bool = False) -> Discovery:
    """Resolve every source into services.

    ``root`` anchors relative paths — the directory holding ``bundle.yml``.
    A source that fails contributes a warning and is skipped; the rest still load.
    """
    specs: list[ServiceSpec] = []
    warnings: list[str] = []

    for source in sources:
        if source.type == "catalog":
            directory = (root / source.path).resolve()
            if not directory.is_dir():
                warnings.append(f"catalog {directory} does not exist")
                continue
            found, catalog_warnings = catalog.load(directory)
            specs.extend(found)
            warnings.extend(catalog_warnings)

        elif source.type == "compose":
            path = (root / source.path).resolve()
            if not path.is_file():
                warnings.append(f"compose file {path} does not exist")
                continue
            try:
                specs.extend(
                    composefile.load_services(
                        path, package=path.parent.name, origin=Origin.COMPOSE
                    )
                )
            except composefile.ComposeError as exc:
                warnings.append(str(exc))

        elif source.type == "container":
            candidate = dockerps.find(source.name or source.ref)
            if candidate is None:
                warnings.append(f"container {source.name or source.ref!r} not found")
                continue
            specs.append(dockerps.to_spec(candidate))

        elif source.type == "image":
            reference = source.ref or source.name
            meta = imageref.inspect(reference, pull=pull)
            if meta is None:
                warnings.append(
                    f"image {reference!r} is not available locally"
                    + ("" if pull else "; pass --pull to fetch it")
                )
            specs.append(imageref.to_spec(reference, meta))

        else:  # pragma: no cover - Manifest validation rejects unknown types
            warnings.append(f"unknown source type {source.type!r}")

    return Discovery(services=_dedupe(specs, warnings), warnings=warnings)


def enrich(specs: list[ServiceSpec], *, pull: bool = False) -> list[str]:
    """Fill gaps in compose-derived services from their images' metadata.

    Mutates the specs in place and returns warnings for images that could not be read.
    """
    warnings: list[str] = []
    cache: dict[str, imageref.ImageMeta | None] = {}

    for spec in specs:
        if not spec.image or spec.origin in (Origin.CONTAINER, Origin.IMAGE):
            continue
        if spec.image not in cache:
            cache[spec.image] = imageref.inspect(spec.image, pull=pull)
        meta = cache[spec.image]
        if meta is None:
            warnings.append(
                f"{spec.slug}: no local metadata for {spec.image}; using the compose file only"
            )
            continue
        imageref.apply_meta(spec, meta)

    return warnings
