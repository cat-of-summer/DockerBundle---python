"""Decide what each bind mount is, and therefore whether it can be baked.

Three kinds matter:

``config``  small files the image should carry — ``php.ini``, ``nginx.conf``
``code``    the application itself, the main thing worth baking
``state``   data that must outlive the image — a database directory, a model cache

Recipes get the first word through their ``mount_kinds`` globs, because they know their
runtime. Everything else falls to heuristics here. When nothing identifies a mount it
stays a volume: baking something we do not understand risks silently freezing data into
an image, which is far worse than an extra volume.
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

from core.model import MountKind, MountMode, MountSpec, ServiceSpec
from recipes.schema import Recipe

#: Directories that hold mutable state in essentially every image that uses them.
STATE_PREFIXES = (
    "/var/lib",
    "/var/log",
    "/var/spool",
    "/data",
    "/qdrant/storage",
    "/root/.ollama",
    "/letsencrypt",
    "/kafka-data",
    "/bitnami",
)

#: Directories that hold the application being served.
CODE_PREFIXES = ("/var/www", "/app", "/srv", "/usr/src", "/opt/app", "/code", "/workspace")

#: Directories that hold configuration.
CONFIG_PREFIXES = ("/etc", "/usr/local/etc", "/conf", "/config")

#: Suffixes that mark a mount source as a single config file rather than a tree.
CONFIG_SUFFIXES = (
    ".conf", ".cnf", ".ini", ".yml", ".yaml", ".json", ".toml", ".properties",
    ".template", ".cfg", ".xml", ".pem", ".crt", ".key",
)

#: Default mode per kind. State stays outside the image; sockets cannot be baked at all.
DEFAULT_MODE = {
    MountKind.CONFIG: MountMode.COPY,
    MountKind.CODE: MountMode.COPY,
    MountKind.STATE: MountMode.VOLUME,
    MountKind.SOCKET: MountMode.SKIP,
    MountKind.UNKNOWN: MountMode.VOLUME,
}


def _under(target: str, prefixes: tuple[str, ...]) -> bool:
    path = PurePosixPath(target)
    for prefix in prefixes:
        candidate = PurePosixPath(prefix)
        if path == candidate or candidate in path.parents:
            return True
    return False


def _recipe_kind(target: str, recipe: Recipe | None) -> str:
    if recipe is None:
        return ""
    # Longest matching glob wins, so "/var/www/html/storage" can override "/var/www/*".
    best = ""
    best_len = -1
    for pattern, kind in recipe.mount_kinds.items():
        if fnmatch.fnmatch(target, pattern) and len(pattern) > best_len:
            best, best_len = str(kind), len(pattern)
    return best


def classify_mount(mount: MountSpec, recipe: Recipe | None = None) -> MountSpec:
    """Set ``kind`` and the default ``mode`` on a single mount, in place."""
    target = mount.target

    if target.endswith(".sock") or mount.kind is MountKind.SOCKET:
        mount.kind = MountKind.SOCKET
        mount.mode = MountMode.SKIP
        return mount

    declared = _recipe_kind(target, recipe)
    if declared == "skip":
        # The bundle supplies its own version of this file.
        mount.kind = MountKind.CONFIG
        mount.mode = MountMode.SKIP
        return mount
    if declared:
        mount.kind = MountKind(declared)
        mount.mode = DEFAULT_MODE[mount.kind]
        return mount

    if mount.named:
        # A named volume is state by construction: it exists to survive the container.
        mount.kind = MountKind.STATE
        mount.mode = MountMode.VOLUME
        return mount

    if _under(target, STATE_PREFIXES):
        mount.kind = MountKind.STATE
    elif target.endswith(CONFIG_SUFFIXES) or mount.source.endswith(CONFIG_SUFFIXES):
        mount.kind = MountKind.CONFIG
    elif _under(target, CODE_PREFIXES):
        mount.kind = MountKind.CODE
    elif _under(target, CONFIG_PREFIXES):
        mount.kind = MountKind.CONFIG
    else:
        mount.kind = MountKind.UNKNOWN

    mount.mode = DEFAULT_MODE[mount.kind]
    return mount


def classify(spec: ServiceSpec, recipe: Recipe | None = None) -> list[MountSpec]:
    """Classify every mount of a service."""
    return [classify_mount(mount, recipe) for mount in spec.mounts]


def apply_overrides(spec: ServiceSpec, overrides: dict[str, str]) -> list[str]:
    """Apply the user's per-mount decisions from ``bundle.yml``.

    Returns warnings for overrides that cannot be honoured — baking a socket, or naming
    a mount target the service does not have.
    """
    warnings: list[str] = []
    by_target = {mount.target: mount for mount in spec.mounts}

    for target, mode in overrides.items():
        mount = by_target.get(target)
        if mount is None:
            warnings.append(f"{spec.slug}: no mount at {target!r} to override")
            continue

        requested = MountMode(mode)
        if requested is MountMode.COPY and not mount.bakeable:
            reason = "a named volume" if mount.named else "a socket"
            warnings.append(
                f"{spec.slug}: {target} cannot be baked because it is {reason}; keeping it a volume"
            )
            continue

        mount.mode = requested

    return warnings
