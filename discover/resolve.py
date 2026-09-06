"""Turn the configuration's ``sources:`` list into a flat set of candidate services."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.manifest import SourceRef
from core.model import Origin, ServiceSpec, normalise_slug
from discover import catalog, composefile, dockerps, imageref


@dataclass
class Discovery:
    services: list[ServiceSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    """Slug collisions the author has to settle. Generation stops on these."""

    def by_slug(self) -> dict[str, ServiceSpec]:
        return {spec.slug: spec for spec in self.services}


def _dedupe(
    specs: list[tuple[ServiceSpec, SourceRef]], warnings: list[str], errors: list[str]
) -> list[ServiceSpec]:
    """Keep one service per slug, refusing collisions the author has to settle.

    Two sources describing *the same thing* — a catalogue folder and the container
    running out of it — legitimately collide, and the first one wins as it always did.

    Two genuinely different services landing on one slug is another matter. ``nginx``
    is the name of a service in five of the stands this tool exists to merge, and
    quietly keeping whichever source was listed first would drop the other from the
    image without it ever appearing in the output. So that is an error, and
    ``sources[].prefix`` is how it is settled.
    """
    seen: dict[str, tuple[ServiceSpec, SourceRef]] = {}
    unique: list[ServiceSpec] = []

    for spec, source in specs:
        previous = seen.get(spec.slug)
        if previous is None:
            seen[spec.slug] = (spec, source)
            unique.append(spec)
            continue

        earlier, earlier_source = previous
        if earlier.package == spec.package and earlier.name == spec.name:
            warnings.append(
                f"slug {spec.slug!r} described by both {earlier.origin.value} "
                f"({earlier_source.label}) and {spec.origin.value} ({source.label}); "
                f"keeping the {earlier.origin.value} one"
            )
            continue

        errors.append(
            f"slug {spec.slug!r} is claimed by two different services: "
            f"{earlier.name!r} from {earlier_source.label} and {spec.name!r} from "
            f"{source.label}. Give one of those sources a prefix: so both survive "
            f"(sources: - {{..., prefix: <name>}})."
        )

    return unique


def _prefixed(spec: ServiceSpec, prefix: str) -> ServiceSpec:
    """Namespace a service's slug, leaving everything else as discovered."""
    if not prefix:
        return spec
    spec.slug = f"{normalise_slug(prefix)}_{spec.slug}"
    return spec


def collect(sources: list[SourceRef], root: Path, *, pull: bool = False) -> Discovery:
    """Resolve every source into services.

    ``root`` anchors relative paths — the directory holding ``docker-bundle.yml``.
    A source that fails contributes a warning and is skipped; the rest still load.
    """
    found: list[tuple[ServiceSpec, SourceRef]] = []
    warnings: list[str] = []
    errors: list[str] = []

    def add(specs: list[ServiceSpec], source: SourceRef) -> None:
        found.extend((_prefixed(spec, source.prefix), source) for spec in specs)

    for source in sources:
        if source.type == "catalog":
            directory = (root / source.path).resolve()
            if not directory.is_dir():
                warnings.append(f"catalog {directory} does not exist")
                continue
            services, catalog_warnings = catalog.load(directory)
            add(services, source)
            warnings.extend(catalog_warnings)

        elif source.type == "compose":
            path = (root / source.path).resolve()
            if not path.is_file():
                warnings.append(f"compose file {path} does not exist")
                continue
            try:
                add(
                    composefile.load_services(
                        path, package=path.parent.name, origin=Origin.COMPOSE
                    ),
                    source,
                )
            except composefile.ComposeError as exc:
                warnings.append(str(exc))

        elif source.type == "container":
            candidate = dockerps.find(source.name or source.ref)
            if candidate is None:
                warnings.append(f"container {source.name or source.ref!r} not found")
                continue
            add([dockerps.to_spec(candidate)], source)

        elif source.type == "image":
            reference = source.ref or source.name
            meta = imageref.inspect(reference, pull=pull)
            if meta is None:
                warnings.append(
                    f"image {reference!r} is not available locally"
                    + ("" if pull else "; pass --pull to fetch it")
                )
            add([imageref.to_spec(reference, meta)], source)

        else:  # pragma: no cover - Manifest validation rejects unknown types
            warnings.append(f"unknown source type {source.type!r}")

    return Discovery(
        services=_dedupe(found, warnings, errors), warnings=warnings, errors=errors
    )


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
