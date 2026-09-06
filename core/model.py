"""Domain model shared by discovery, planning and rendering.

Everything here is plain data. Discovery produces :class:`ServiceSpec` objects from
four different kinds of source; the planner turns those plus recipes into a
:class:`BundlePlan`; the renderer walks the plan and writes files. Nothing in this
module touches the filesystem or Docker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

_SLUG_INVALID = re.compile(r"[^a-z0-9]+")


def normalise_slug(raw: str) -> str:
    """Collapse an arbitrary name into ``[a-z0-9_]+``.

    Package directories in the wild look like ``"5. hf-text"`` or ``"node(backend)-nginx"``;
    the result has to be usable as a supervisord program name, a Dockerfile stage name and
    an environment-variable prefix all at once.
    """
    slug = _SLUG_INVALID.sub("_", raw.strip().lower()).strip("_")
    if not slug:
        return "service"
    if slug[0].isdigit():
        slug = f"s_{slug}"
    return slug


def env_prefix(slug: str) -> str:
    """Environment-variable prefix for a service, e.g. ``laravel`` -> ``LARAVEL``."""
    return normalise_slug(slug).upper()


def dockerfile_path(source_dir: Path | None, build: dict | None) -> Path | None:
    """Resolve a compose ``build:`` block to the Dockerfile it names.

    Both parts are relative to the directory the service's paths resolve against, which
    for an included file is its ``project_directory`` rather than its own folder.
    """
    if build is None or source_dir is None:
        return None
    context = source_dir / str(build.get("context", "."))
    return context / str(build.get("dockerfile", "Dockerfile"))


class Origin(str, Enum):
    """Where a service was discovered."""

    CATALOG = "catalog"  # a directory of package folders, each with a compose file
    COMPOSE = "compose"  # a single compose file with several services
    CONTAINER = "container"  # a live container reported by the daemon
    IMAGE = "image"  # a bare image reference typed by the user


class ServiceMode(str, Enum):
    """What becomes of a discovered service.

    One axis instead of the two it used to take. ``enabled`` and ``bakeable`` were
    independent flags describing the same decision, which made "outside the image but
    still configurable" impossible to state: turning a service off took its environment
    keys with it, and the deployment lost the ``DB_HOST`` it needed to reach the database
    that was living elsewhere on purpose.
    """

    BAKE = "bake"  # a process inside the bundle image
    SIDECAR = "sidecar"  # its own service in the generated compose file
    EXTERNAL = "external"  # rendered nowhere, but its .env keys survive
    OFF = "off"  # dropped, keys and all


class MountKind(str, Enum):
    """What a bind mount actually carries.

    Drives the default of :attr:`MountSpec.mode`: configuration and code are safe to bake
    into the image, state must survive image upgrades, and sockets can never be baked.
    """

    CONFIG = "config"
    CODE = "code"
    STATE = "state"
    SOCKET = "socket"
    UNKNOWN = "unknown"


class MountMode(str, Enum):
    COPY = "copy"  # baked into the image with COPY
    VOLUME = "volume"  # stays a volume on the generated compose service
    SKIP = "skip"  # dropped entirely


@dataclass
class MountSpec:
    """A single entry from a compose ``volumes:`` list."""

    source: str
    target: str
    kind: MountKind = MountKind.UNKNOWN
    mode: MountMode = MountMode.VOLUME
    read_only: bool = False
    named: bool = False
    """True when the source was a named volume rather than a host path."""

    @property
    def bakeable(self) -> bool:
        return not self.named and self.kind is not MountKind.SOCKET


@dataclass
class PortSpec:
    """A port the service listens on, plus how it reaches the outside world."""

    container: int
    """Port inside the bundle. May differ from :attr:`original` after remapping."""

    original: int
    published: str = ""
    """Host-side publish spec as written in compose, e.g. ``127.0.0.1:8081``."""

    protocol: str = "tcp"

    @property
    def remapped(self) -> bool:
        return self.container != self.original


@dataclass
class EnvVar:
    """One assignment from a ``.env.example``, with its leading comment preserved."""

    key: str
    value: str
    comment: str = ""
    source: str = ""
    """Slug of the service this came from; empty for globals."""


@dataclass
class ServiceSpec:
    """A single candidate for baking, normalised across all four discovery sources."""

    slug: str
    name: str
    """Service name as written in the source compose file."""

    package: str
    """Package directory name, compose project name, or image name."""

    origin: Origin
    source_dir: Path | None = None
    """Directory holding the Dockerfile, configs and entrypoint, when there is one."""

    image: str | None = None
    base_image: str | None = None
    """For ``build:`` services, the image their Dockerfile's final stage derives from.

    Recipe matching falls back to this, since such a service has no ``image:`` of its own
    but its Dockerfile still says whether it is PHP, Node or something else.
    """

    build: dict | None = None
    command: list[str] | None = None
    entrypoint: list[str] | None = None
    working_dir: str | None = None
    user: str | None = None

    environment: dict[str, str] = field(default_factory=dict)
    raw_environment: dict[str, str] = field(default_factory=dict)
    """``environment:`` before interpolation, e.g. ``{"DB_HOST": "${DB_HOST}"}``.

    Needed to rebuild the link between a process variable and the ``.env`` key it reads.
    Once a key is renamed to avoid a collision, supervisord has to map it back so the
    application still sees the name it expects.
    """

    env_vars: list[EnvVar] = field(default_factory=list)
    mounts: list[MountSpec] = field(default_factory=list)
    ports: list[PortSpec] = field(default_factory=list)
    raw_ports: list[str] = field(default_factory=list)
    """``ports:`` before interpolation, e.g. ``["${EXTERNAL_ACCESS}"]``.

    Packages publish through a single variable holding the whole spec. Resolving it at
    generation time would freeze the host port into the image's compose file, so the
    deployment's ``.env`` could no longer move it — the one thing that file is for.
    """

    depends_on: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    raw_labels: dict[str, str] = field(default_factory=dict)
    """``labels:`` before interpolation, e.g. ``Host(`${TRAEFIK_DOMAIN}`)``.

    The bundle re-emits these on its own container, and it must re-emit them as written:
    a routing domain baked to a literal at generation time could no longer be changed
    from the deployment's ``.env``, which is the whole point of shipping one.
    """

    healthcheck: dict | None = None

    privileged: bool = False
    network_mode: str | None = None
    restart: str = ""
    shm_size: str = ""
    """``shm_size:`` as written, e.g. ``${PLAYWRIGHT_SHM_SIZE:-2gb}``.

    A container-wide setting, but one a single service can require: Chromium puts its
    render surfaces in ``/dev/shm`` and crashes part-way through a long page on Docker's
    64 MB default. Losing it turns a working stand into one that fails intermittently.
    """

    @property
    def env_prefix(self) -> str:
        return env_prefix(self.slug)

    @property
    def effective_image(self) -> str:
        """The image reference recipe matching should consider."""
        return self.image or self.base_image or ""

    @property
    def dockerfile(self) -> Path | None:
        """Where this service's Dockerfile lives, for a ``build:`` service."""
        return dockerfile_path(self.source_dir, self.build)

    @property
    def declared_labels(self) -> dict[str, str]:
        """Labels as the author wrote them, falling back to the interpolated copy.

        Sources other than a compose file (a live container, a bare image) only ever have
        the resolved form.
        """
        return self.raw_labels or self.labels

    def find_entrypoint(self) -> Path | None:
        """The service's own entrypoint script, if the package ships one.

        Looked for beside the Dockerfile first and only then in the package root, because
        a package that splits into ``services/<name>/`` keeps the two together and
        qualifies the filename — ``entrypoint.nginx.sh``, not ``entrypoint.sh``. Anchoring
        on the Dockerfile keeps this independent of how the package is laid out.
        """
        names = (f"entrypoint.{self.name}.sh", f"entrypoint.{self.slug}.sh", "entrypoint.sh")
        directories: list[Path] = []
        dockerfile = self.dockerfile
        if dockerfile is not None:
            directories.append(dockerfile.parent)
        if self.source_dir is not None and self.source_dir not in directories:
            directories.append(self.source_dir)

        for directory in directories:
            for name in names:
                candidate = directory / name
                if candidate.is_file():
                    return candidate
        return None

    def mount_for(self, target: str) -> MountSpec | None:
        for mount in self.mounts:
            if mount.target == target:
                return mount
        return None


@dataclass
class CopyOp:
    """A single ``COPY`` in the generated Dockerfile.

    :attr:`context` is the path inside ``dist/context/`` that the writer stages the
    source into, so the build context stays self-contained.
    """

    context: str
    target: str
    source: Path
    kind: MountKind = MountKind.CONFIG
    chmod: str = ""

    from_image: str = ""
    """Image to take the content from, instead of the build context."""

    from_path: str = ""
    """Path inside :attr:`from_image`."""

    from_stage: str = ""
    """Build stage the planner assigned to :attr:`from_image`."""


@dataclass
class SupervisorProgram:
    """One ``[program:...]`` block."""

    name: str
    command: str
    priority: int = 50
    directory: str = ""
    environment: dict[str, str] = field(default_factory=dict)
    stopsignal: str = "TERM"
    stopwaitsecs: int = 10
    autorestart: bool = True
    startsecs: int = 1
    autostart: bool = True
    """False for programs the entrypoint starts by hand after initialisation.

    Application processes must not come up before their service's own entrypoint has run
    — php-fpm with no vendor/ directory just crash-loops. Data services keep
    ``autostart`` so that those init scripts have a database to migrate against.
    """
    replicas_var: str = ""
    """Name of the env var driving ``numprocs``; empty means a single process."""

    scalable: bool = True
    """False for port-bound programs, where extra processes would collide."""

    critical: bool = True
    """Whether healthcheck.sh requires this program to be RUNNING."""


@dataclass
class ReadinessProbe:
    kind: str  # "tcp" | "http" | "command"
    target: str
    timeout: int = 30
    label: str = ""


@dataclass
class PlannedService:
    """A :class:`ServiceSpec` resolved against a recipe and assigned concrete resources."""

    spec: ServiceSpec
    recipe_name: str
    bakeable: bool
    priority: int = 50
    programs: list[SupervisorProgram] = field(default_factory=list)
    copies: list[CopyOp] = field(default_factory=list)
    ports: list[PortSpec] = field(default_factory=list)
    volumes: list[MountSpec] = field(default_factory=list)
    readiness: list[ReadinessProbe] = field(default_factory=list)
    init_script: str = ""
    """Path inside the image of the service's own entrypoint, run once before supervisord."""

    init_cwd: str = ""
    """Directory the service's own entrypoint runs in.

    Not the compose ``working_dir``: baking relocates the code from the shared
    ``/var/www/html`` to a per-service directory, and a script that checks for
    ``artisan`` in the current directory has to be run where the code actually landed.
    """

    init_env: dict[str, str] = field(default_factory=dict)
    """Shell assignments exported around :attr:`init_script`.

    Restores the variable names a service's own entrypoint expects when a collision
    forced them to be renamed in the merged ``.env``. This is the only place per-service
    values can be applied for a shared runtime, whose single process cannot hold
    conflicting values for several services at once.
    """

    build_steps: list[str] = field(default_factory=list)
    """``RUN`` lines emitted after this service's ``COPY`` block."""

    pre_init: list[str] = field(default_factory=list)
    """Commands run just before this service's own entrypoint."""

    post_init: list[str] = field(default_factory=list)
    """Commands run immediately after this service's own entrypoint.

    Interleaved per service rather than collected to the end: two nginx services both
    write ``conf.d/default.conf``, so the first result must be filed away under its own
    name before the second entrypoint runs.
    """

    stage: str = ""
    """Dockerfile build stage name, when the recipe pulls the upstream image in."""

    rootfs_import: bool = False
    install: list[str] = field(default_factory=list)


@dataclass
class BundlePlan:
    """Everything the renderer needs. Produced by :mod:`plan.builder`."""

    name: str
    baked: list[PlannedService] = field(default_factory=list)
    sidecars: list[PlannedService] = field(default_factory=list)
    external: list[ServiceSpec] = field(default_factory=list)
    """Services deliberately left outside the bundle, kept only for their ``.env`` keys.

    A database that already runs elsewhere is not part of the image and not part of the
    generated compose file, but the deployment still has to be told where to find it.
    Dropping it outright would take ``DB_HOST`` and friends out of ``.env.example`` too.
    """

    features: dict[str, bool] = field(default_factory=dict)
    """Feature flags this plan was built with, as resolved from the file and the flags."""

    env: list[EnvVar] = field(default_factory=list)
    named_volumes: dict[str, str] = field(default_factory=dict)
    """Volume name -> mount target inside the bundle container."""

    network: str = "network"
    network_external: bool = True
    base_images: dict[str, str] = field(default_factory=dict)
    """Variant name -> base image, e.g. ``{"cpu": "debian:bookworm-slim"}``."""

    warnings: list[str] = field(default_factory=list)
    port_map: dict[str, list[PortSpec]] = field(default_factory=dict)

    programs: list[SupervisorProgram] = field(default_factory=list)
    """Every supervisord program, already ordered by priority."""

    install: dict[str, list[str]] = field(default_factory=dict)
    """Base family -> the deduplicated union of packages every recipe asked for."""

    run_steps: dict[str, list[str]] = field(default_factory=dict)
    """Base family -> extra RUN commands, in recipe order and deduplicated."""

    stages: list[tuple[str, str]] = field(default_factory=list)
    """``(stage name, image)`` pairs every ``FROM`` in the Dockerfile needs declared."""

    rootfs_stages: list[str] = field(default_factory=list)
    """Stages whose whole filesystem is merged into the base.

    A subset of :attr:`stages`: a stage that only serves a ``COPY --from`` must not be
    merged, or lifting one directory out of an image would drag the rest in anyway.
    """

    readiness: list[ReadinessProbe] = field(default_factory=list)
    post_init: list[str] = field(default_factory=list)
    env_renames: dict[str, dict[str, str]] = field(default_factory=dict)
    family: str = "debian"

    deferred: list[str] = field(default_factory=list)
    """Programs the entrypoint starts by hand once initialisation has finished."""

    @property
    def services(self) -> list[PlannedService]:
        return [*self.baked, *self.sidecars]

    @property
    def has_rootfs_import(self) -> bool:
        return any(service.rootfs_import for service in self.baked)

    @property
    def needs_init(self) -> bool:
        """Whether the entrypoint has to run an initialisation phase.

        Both the planner (deciding which programs to defer) and the renderer (emitting
        the phased entrypoint) ask this. They must agree: a program released in phase 3
        while the entrypoint never reaches phase 3 would never start, and a program
        autostarted in phase 1 would run before the config its ``post_init`` installs.
        """
        return any(
            service.init_script or service.pre_init or service.post_init
            for service in self.baked
        )
