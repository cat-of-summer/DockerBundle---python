"""Image metadata, used both as a source and to enrich compose-derived services.

``docker image inspect`` is the authoritative answer to "what does this container
actually run and on which port" — a compose file often says neither, because the
upstream image's ``ENTRYPOINT`` and ``EXPOSE`` already do. When the image is not
available locally and pulling is not allowed, we fall back to the compose file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.model import Origin, PortSpec, ServiceSpec, normalise_slug
from discover.dockerclient import DockerUnavailable, client


@dataclass
class ImageMeta:
    """The subset of ``docker image inspect`` that affects a bundle."""

    reference: str
    entrypoint: list[str] = field(default_factory=list)
    cmd: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    working_dir: str = ""
    user: str = ""
    exposed_ports: list[PortSpec] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    digest: str = ""

    @property
    def start_command(self) -> list[str]:
        """The command Docker would run: entrypoint followed by cmd."""
        return [*self.entrypoint, *self.cmd]


def _split_env(raw: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw or []:
        key, sep, value = str(item).partition("=")
        if sep:
            result[key] = value
    return result


def _exposed(raw: dict | None) -> list[PortSpec]:
    ports: list[PortSpec] = []
    for spec in raw or {}:
        number, _, protocol = str(spec).partition("/")
        if number.isdigit():
            ports.append(
                PortSpec(container=int(number), original=int(number), protocol=protocol or "tcp")
            )
    return sorted(ports, key=lambda p: p.container)


def from_attrs(reference: str, attrs: dict) -> ImageMeta:
    """Build :class:`ImageMeta` from a raw inspect payload."""
    config = attrs.get("Config") or {}
    digests = attrs.get("RepoDigests") or []
    return ImageMeta(
        reference=reference,
        entrypoint=[str(x) for x in (config.get("Entrypoint") or [])],
        cmd=[str(x) for x in (config.get("Cmd") or [])],
        env=_split_env(config.get("Env")),
        working_dir=str(config.get("WorkingDir") or ""),
        user=str(config.get("User") or ""),
        exposed_ports=_exposed(config.get("ExposedPorts")),
        volumes=sorted(str(v) for v in (config.get("Volumes") or {})),
        labels={str(k): str(v) for k, v in (config.get("Labels") or {}).items()},
        digest=str(digests[0]) if digests else str(attrs.get("Id") or ""),
    )


def inspect(reference: str, *, pull: bool = False) -> ImageMeta | None:
    """Inspect an image, optionally pulling it first.

    Returns ``None`` when the daemon is unreachable or the image cannot be obtained;
    callers treat that as "fall back to the compose file".
    """
    try:
        api = client()
    except DockerUnavailable:
        return None

    try:
        image = api.images.get(reference)
    except Exception:
        if not pull:
            return None
        try:
            image = api.images.pull(reference)
        except Exception:
            return None
        if isinstance(image, list):
            if not image:
                return None
            image = image[0]

    return from_attrs(reference, image.attrs or {})


def to_spec(reference: str, meta: ImageMeta | None = None) -> ServiceSpec:
    """Turn a bare image reference into a service candidate."""
    name = reference.rsplit("/", 1)[-1].split(":")[0].split("@")[0]
    slug = normalise_slug(name)

    spec = ServiceSpec(
        slug=slug,
        name=name,
        package=reference,
        origin=Origin.IMAGE,
        image=reference,
    )
    if meta is not None:
        apply_meta(spec, meta)
    return spec


def apply_meta(spec: ServiceSpec, meta: ImageMeta) -> ServiceSpec:
    """Fill gaps in ``spec`` from image metadata, without overwriting explicit values.

    The compose file always wins where it says something: it is the author's intent for
    this deployment. The image only supplies what compose left unsaid.
    """
    if spec.entrypoint is None and meta.entrypoint:
        spec.entrypoint = list(meta.entrypoint)
    if spec.command is None and meta.cmd:
        spec.command = list(meta.cmd)
    if not spec.working_dir and meta.working_dir:
        spec.working_dir = meta.working_dir
    if not spec.user and meta.user:
        spec.user = meta.user

    for key, value in meta.env.items():
        spec.environment.setdefault(key, value)

    known = {port.original for port in spec.ports}
    for port in meta.exposed_ports:
        if port.original not in known:
            spec.ports.append(
                PortSpec(
                    container=port.container,
                    original=port.original,
                    published="",
                    protocol=port.protocol,
                )
            )

    return spec
