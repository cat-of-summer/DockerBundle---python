"""Discover candidates from containers the daemon already knows about.

Useful on a server where the stack is running but its compose files are elsewhere: a
container's inspect payload carries the resolved image, the effective command, the real
bind mounts and the actual port bindings. Compose labels, when present, tell us which
project and service the container belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.model import (
    MountKind,
    MountSpec,
    Origin,
    PortSpec,
    ServiceSpec,
    normalise_slug,
)
from discover.dockerclient import DockerUnavailable, client
from discover.imageref import from_attrs

LABEL_PROJECT = "com.docker.compose.project"
LABEL_SERVICE = "com.docker.compose.service"
LABEL_WORKING_DIR = "com.docker.compose.project.working_dir"


@dataclass
class ContainerCandidate:
    """A running or stopped container offered to the user for selection."""

    id: str
    name: str
    image: str
    status: str
    project: str
    service: str
    attrs: dict

    @property
    def label(self) -> str:
        if self.project and self.service:
            return f"{self.project}/{self.service}"
        return self.name


def list_containers(*, all_containers: bool = True) -> list[ContainerCandidate]:
    """List containers, newest first. Returns an empty list when Docker is unreachable."""
    try:
        api = client()
    except DockerUnavailable:
        return []

    try:
        containers = api.containers.list(all=all_containers)
    except Exception:
        return []

    candidates: list[ContainerCandidate] = []
    for container in containers:
        attrs = container.attrs or {}
        labels = (attrs.get("Config") or {}).get("Labels") or {}
        candidates.append(
            ContainerCandidate(
                id=str(attrs.get("Id", ""))[:12],
                name=str(container.name),
                image=str((attrs.get("Config") or {}).get("Image") or ""),
                status=str(attrs.get("State", {}).get("Status", "")),
                project=str(labels.get(LABEL_PROJECT, "")),
                service=str(labels.get(LABEL_SERVICE, "")),
                attrs=attrs,
            )
        )

    candidates.sort(key=lambda c: (c.project, c.service, c.name))
    return candidates


def _mounts_from_attrs(attrs: dict) -> list[MountSpec]:
    mounts: list[MountSpec] = []
    for entry in attrs.get("Mounts") or []:
        target = str(entry.get("Destination") or "")
        if not target:
            continue
        kind = MountKind.SOCKET if target.endswith(".sock") else MountKind.UNKNOWN
        mounts.append(
            MountSpec(
                source=str(entry.get("Source") or entry.get("Name") or ""),
                target=target,
                kind=kind,
                read_only=not entry.get("RW", True),
                named=str(entry.get("Type") or "") == "volume",
            )
        )
    return mounts


def _ports_from_attrs(attrs: dict) -> list[PortSpec]:
    config = attrs.get("Config") or {}
    host_config = attrs.get("HostConfig") or {}
    bindings = host_config.get("PortBindings") or {}

    ports: list[PortSpec] = []
    seen: set[int] = set()

    for spec in list(bindings.keys()) + list((config.get("ExposedPorts") or {}).keys()):
        number, _, protocol = str(spec).partition("/")
        if not number.isdigit() or int(number) in seen:
            continue
        seen.add(int(number))

        published = ""
        for binding in bindings.get(spec) or []:
            host_ip = str(binding.get("HostIp") or "")
            host_port = str(binding.get("HostPort") or "")
            published = f"{host_ip}:{host_port}" if host_ip else host_port
            break

        ports.append(
            PortSpec(
                container=int(number),
                original=int(number),
                published=published,
                protocol=protocol or "tcp",
            )
        )

    return sorted(ports, key=lambda p: p.container)


def to_spec(candidate: ContainerCandidate) -> ServiceSpec:
    """Build a :class:`ServiceSpec` from a container's inspect payload."""
    attrs = candidate.attrs
    config = attrs.get("Config") or {}
    host_config = attrs.get("HostConfig") or {}
    labels = config.get("Labels") or {}

    package = candidate.project or candidate.name
    service = candidate.service or candidate.name
    slug = normalise_slug(f"{package}_{service}" if candidate.project else service)

    working_dir = labels.get(LABEL_WORKING_DIR)
    source_dir = Path(working_dir) if working_dir and Path(working_dir).is_dir() else None

    meta = from_attrs(str(config.get("Image") or candidate.image), attrs)

    restart_policy = (host_config.get("RestartPolicy") or {}).get("Name") or ""
    network_mode = str(host_config.get("NetworkMode") or "")

    return ServiceSpec(
        slug=slug,
        name=service,
        package=package,
        origin=Origin.CONTAINER,
        source_dir=source_dir,
        image=str(config.get("Image") or candidate.image) or None,
        command=list(meta.cmd) or None,
        entrypoint=list(meta.entrypoint) or None,
        working_dir=meta.working_dir or None,
        user=meta.user or None,
        environment=dict(meta.env),
        mounts=_mounts_from_attrs(attrs),
        ports=_ports_from_attrs(attrs),
        labels={str(k): str(v) for k, v in labels.items()},
        healthcheck=(
            config.get("Healthcheck") if isinstance(config.get("Healthcheck"), dict) else None
        ),
        privileged=bool(host_config.get("Privileged", False)),
        network_mode=network_mode or None,
        restart=restart_policy,
    )


def find(name_or_id: str) -> ContainerCandidate | None:
    for candidate in list_containers():
        if name_or_id in (candidate.name, candidate.id) or candidate.id.startswith(name_or_id):
            return candidate
    return None
