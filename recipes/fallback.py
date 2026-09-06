"""Generate a recipe for an image no hand-written recipe claims.

The bundle has to work for arbitrary images — a private ``ghcr.io/acme/svc``, a
``clickhouse:24`` nobody wrote a recipe for. For those we import the upstream image's
filesystem into the bundle and run its own ``ENTRYPOINT``/``CMD`` under supervisord.

The import is a merge rather than a plain ``COPY --from=svc / /``: overwriting the base
image's ``/etc/passwd``, ``/etc/nsswitch.conf`` and friends would break every *other*
service in the bundle. ``render/assets/merge-rootfs.sh`` performs the merge, keeping
files that already exist and appending only missing accounts.

Two costs, both documented for the user: the resulting image is large, and the port a
rootfs-imported service listens on cannot be changed, because we have no idea where it
is configured.
"""

from __future__ import annotations

import shlex

from core.model import ServiceSpec, normalise_slug
from recipes.schema import PortRule, ProgramRule, ReadinessRule, Recipe

#: Directories that are never worth importing: kernel interfaces and scratch space.
EXCLUDED_PATHS = ("./proc", "./sys", "./dev", "./tmp", "./run", "./var/run")

#: Files owned by the base image. Keeping the base's copy is what makes the merge safe.
PRESERVED_FILES = (
    "./etc/passwd",
    "./etc/group",
    "./etc/shadow",
    "./etc/hosts",
    "./etc/hostname",
    "./etc/resolv.conf",
    "./etc/nsswitch.conf",
    "./etc/localtime",
)


def stage_name(slug: str) -> str:
    """Dockerfile build-stage name for an imported image."""
    return f"svc_{normalise_slug(slug)}"


def start_command(spec: ServiceSpec) -> str:
    """The command Docker itself would run for this service.

    Compose ``entrypoint``/``command`` win where set; otherwise the image's own metadata,
    which :mod:`discover.imageref` has already merged into the spec.
    """
    parts = [*(spec.entrypoint or []), *(spec.command or [])]
    if not parts:
        return ""
    return shlex.join(parts)


def build(spec: ServiceSpec) -> Recipe:
    """Produce a rootfs-import recipe for ``spec``."""
    slug = normalise_slug(spec.slug)
    command = start_command(spec)

    if not command:
        # Nothing to run: no compose command and no image metadata. Emit a program that
        # fails loudly at start-up rather than a silently broken image.
        command = (
            f"sh -c 'echo \"[{slug}] no start command known; "
            "set one in docker-bundle.yml or write a recipe\" >&2; exit 1'"
        )

    port_rule = None
    readiness: list[ReadinessRule] = []
    if spec.ports:
        first = spec.ports[0].original
        port_rule = PortRule(default=first, mechanism="none")
        readiness = [ReadinessRule(type="tcp", target="{port}", timeout=60)]

    program = ProgramRule(
        name=slug,
        command=command,
        directory=spec.working_dir or "",
        priority=50,
        stopsignal="TERM",
        stopwaitsecs=30,
        # An imported service is opaque: assume it binds its port and must stay single.
        scalable=not spec.ports,
        critical=True,
    )

    return Recipe(
        name=f"auto:{slug}",
        priority=0,
        bakeable=_is_bakeable(spec),
        reason=_unbakeable_reason(spec),
        install={},
        port=port_rule,
        programs=[program],
        readiness=readiness,
    )


def _is_bakeable(spec: ServiceSpec) -> bool:
    return not _unbakeable_reason(spec)


def _unbakeable_reason(spec: ServiceSpec) -> str:
    """Explain why a service cannot live inside the bundle, or return an empty string.

    These are hard blockers rather than preferences: a container that needs the Docker
    socket or the host network is coordinating *other* containers and cannot be one of
    the processes it manages.
    """
    if spec.privileged:
        return "runs privileged"
    if spec.network_mode and spec.network_mode not in ("bridge", "default", ""):
        return f"uses network_mode: {spec.network_mode}"
    for mount in spec.mounts:
        if mount.target.endswith("docker.sock"):
            return "mounts the Docker socket"
    return ""
