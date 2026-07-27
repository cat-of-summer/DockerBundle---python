"""Read a Compose file into :class:`~core.model.ServiceSpec` objects.

Handles both the short and long syntax for ``volumes:``, ``ports:`` and ``depends_on:``,
and runs the whole document through :mod:`discover.interpolate` first so that
``image: mysql:${MYSQL_VERSION}`` resolves to a real image reference.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from core.model import (
    EnvVar,
    MountKind,
    MountSpec,
    Origin,
    PortSpec,
    ServiceSpec,
    normalise_slug,
)
from discover import dockerfile as dockerfile_mod
from discover import envfile
from discover.interpolate import interpolate_tree

#: Recognised names for the file holding a compose project, in preference order.
COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")

OVERRIDE_NAMES = (
    "compose.override.yaml",
    "compose.override.yml",
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
)

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


class ComposeError(ValueError):
    """Raised for a compose file we cannot make sense of."""


# ---------------------------------------------------------------------------
# scalar helpers
# ---------------------------------------------------------------------------


def split_volume(spec: str) -> tuple[str, str, str]:
    """Split ``source:target[:mode]``, tolerating Windows drive letters.

    ``C:\\src:/app:ro`` must not be split on the drive colon, so the leading drive is
    detached before splitting and re-attached afterwards.
    """
    prefix = ""
    rest = spec
    if _WINDOWS_DRIVE.match(spec):
        prefix, rest = spec[:2], spec[2:]

    parts = rest.split(":")
    if len(parts) == 1:
        return "", prefix + parts[0], ""
    if len(parts) == 2:
        return prefix + parts[0], parts[1], ""
    return prefix + parts[0], parts[1], ":".join(parts[2:])


def is_bind_source(source: str) -> bool:
    """True when a volume source is a host path rather than a named volume."""
    if not source:
        return False
    return (
        source.startswith(("./", "../", "/", "~", ".\\", "..\\"))
        or source in (".", "..")
        or bool(_WINDOWS_DRIVE.match(source))
    )


def parse_port(spec: Any) -> PortSpec | None:
    """Parse one ``ports:`` entry into a :class:`PortSpec`.

    Returns ``None`` for entries that carry no usable container port — most often an
    unset ``${EXTERNAL_ACCESS}`` that interpolated away to an empty string.
    """
    if isinstance(spec, dict):
        target = spec.get("target")
        if target is None:
            return None
        published = spec.get("published", "")
        host_ip = spec.get("host_ip", "")
        published_text = f"{host_ip}:{published}" if host_ip else str(published or "")
        return PortSpec(
            container=int(target),
            original=int(target),
            published=published_text,
            protocol=str(spec.get("protocol", "tcp")),
        )

    text = str(spec).strip()
    if not text:
        return None

    protocol = "tcp"
    if "/" in text:
        text, _, protocol = text.rpartition("/")
        protocol = protocol or "tcp"

    parts = text.split(":")
    container_part = parts[-1]
    # Ranges such as 8000-8010 are collapsed to their first port; the bundle exposes a
    # single process, and a range would make port remapping meaningless.
    container_part = container_part.split("-")[0]
    if not container_part.isdigit():
        return None

    container = int(container_part)
    published = ":".join(parts[:-1])
    return PortSpec(container=container, original=container, published=published, protocol=protocol)


def _as_mapping(value: Any) -> dict[str, str]:
    """Normalise the ``list | mapping`` duality compose allows for env and labels."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): "" if v is None else str(v) for k, v in value.items()}
    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in value:
            key, sep, val = str(item).partition("=")
            result[key.strip()] = val if sep else ""
        return result
    return {}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(k) for k in value]
    return [str(item) for item in value]


def _command_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


# ---------------------------------------------------------------------------
# document loading
# ---------------------------------------------------------------------------


def find_compose_file(directory: Path) -> Path | None:
    for name in COMPOSE_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def find_override_file(directory: Path) -> Path | None:
    for name in OVERRIDE_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def deep_merge(base: dict, overlay: dict) -> dict:
    """Merge an override document over a base one, the way compose does.

    Mappings merge recursively; lists and scalars are replaced wholesale.
    """
    result = dict(base)
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = value
    return result


def load_document(
    path: Path,
    values: dict[str, str],
    *,
    with_override: bool = True,
    keep_placeholders: bool = False,
) -> dict:
    """Load, merge and interpolate a compose document.

    ``keep_placeholders`` skips interpolation entirely, yielding the document as written.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ComposeError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ComposeError(f"{path} is not valid YAML: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ComposeError(f"{path}: expected a mapping at the top level")

    if with_override:
        override_path = find_override_file(path.parent)
        if override_path is not None:
            try:
                overlay = yaml.safe_load(override_path.read_text(encoding="utf-8-sig")) or {}
            except (OSError, yaml.YAMLError):
                overlay = {}
            if isinstance(overlay, dict):
                raw = deep_merge(raw, overlay)

    if keep_placeholders:
        return raw
    return interpolate_tree(raw, values)


# ---------------------------------------------------------------------------
# service extraction
# ---------------------------------------------------------------------------


def _mounts_from(raw: Any, declared_volumes: set[str]) -> list[MountSpec]:
    mounts: list[MountSpec] = []
    for item in raw or []:
        if isinstance(item, dict):
            source = str(item.get("source", ""))
            target = str(item.get("target", ""))
            named = str(item.get("type", "")) == "volume" or source in declared_volumes
            read_only = bool(item.get("read_only", False))
        else:
            source, target, mode = split_volume(str(item))
            if not target:
                continue
            named = bool(source) and not is_bind_source(source)
            read_only = "ro" in mode.split(",")

        if not target:
            continue

        mounts.append(
            MountSpec(
                source=source,
                target=target,
                kind=MountKind.UNKNOWN,
                read_only=read_only,
                named=named,
            )
        )
    return mounts


def _depends_on(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        return [str(key) for key in raw]
    return _as_list(raw)


def service_from_mapping(
    *,
    slug: str,
    name: str,
    package: str,
    raw: dict,
    origin: Origin,
    source_dir: Path | None,
    declared_volumes: set[str],
    env_vars: list[EnvVar],
    raw_environment: dict[str, str] | None = None,
) -> ServiceSpec:
    ports: list[PortSpec] = []
    for entry in raw.get("ports") or []:
        port = parse_port(entry)
        if port is not None:
            ports.append(port)

    # `expose:` also declares a listening port, but without publishing it.
    for entry in raw.get("expose") or []:
        port = parse_port(entry)
        if port is not None and all(p.container != port.container for p in ports):
            port.published = ""
            ports.append(port)

    build = raw.get("build")
    if isinstance(build, str):
        build = {"context": build}
    build = build if isinstance(build, dict) else None

    # A `build:` service has no image to match recipes against; its Dockerfile does.
    base = ""
    if build is not None and source_dir is not None:
        context = source_dir / str(build.get("context", "."))
        dockerfile = context / str(build.get("dockerfile", "Dockerfile"))
        if dockerfile.is_file():
            args = {str(k): str(v) for k, v in _as_mapping(build.get("args")).items()}
            base = dockerfile_mod.base_image(dockerfile, args)

    return ServiceSpec(
        slug=slug,
        name=name,
        package=package,
        origin=origin,
        source_dir=source_dir,
        image=str(raw["image"]) if raw.get("image") else None,
        base_image=base or None,
        build=build,
        command=_command_list(raw.get("command")),
        entrypoint=_command_list(raw.get("entrypoint")),
        working_dir=str(raw["working_dir"]) if raw.get("working_dir") else None,
        user=str(raw["user"]) if raw.get("user") else None,
        environment=_as_mapping(raw.get("environment")),
        raw_environment=dict(raw_environment or {}),
        env_vars=env_vars,
        mounts=_mounts_from(raw.get("volumes"), declared_volumes),
        ports=ports,
        depends_on=_depends_on(raw.get("depends_on")),
        labels=_as_mapping(raw.get("labels")),
        healthcheck=raw.get("healthcheck") if isinstance(raw.get("healthcheck"), dict) else None,
        privileged=bool(raw.get("privileged", False)),
        network_mode=str(raw["network_mode"]) if raw.get("network_mode") else None,
        restart=str(raw.get("restart", "")),
    )


def load_services(
    path: Path,
    *,
    package: str = "",
    slug_base: str = "",
    origin: Origin = Origin.COMPOSE,
    extra_values: dict[str, str] | None = None,
) -> list[ServiceSpec]:
    """Load every service from a compose file.

    Values for interpolation come from a sibling ``.env`` (preferred) or ``.env.example``,
    overlaid with ``extra_values``. The same entries are attached to each service as its
    :attr:`ServiceSpec.env_vars`, so the planner can merge them into ``dist/.env.example``.

    ``slug_base`` overrides the stem used to build slugs; catalogues pass the package name
    with its ordering prefix stripped, so ``3. mysql`` yields ``mysql`` rather than
    ``s_3_mysql``. :attr:`ServiceSpec.package` keeps the real directory name either way.
    """
    directory = path.parent
    package = package or directory.name
    package_slug = normalise_slug(slug_base or package)

    env = envfile.load_optional(directory, ".env", ".env.example", source=package_slug)
    values = env.as_dict()
    if extra_values:
        values.update(extra_values)

    document = load_document(path, values)
    services = document.get("services") or {}
    if not isinstance(services, dict):
        raise ComposeError(f"{path}: services must be a mapping")

    # A second, un-interpolated pass keeps `environment: DB_HOST: ${DB_HOST}` intact, so
    # the planner can still tell which .env key each process variable reads.
    raw_document = load_document(path, {}, keep_placeholders=True)
    raw_services = raw_document.get("services") or {}

    declared_volumes = set(document.get("volumes") or {})
    multi = len(services) > 1

    specs: list[ServiceSpec] = []
    for name, raw in services.items():
        if not isinstance(raw, dict):
            continue
        name = str(name)
        # A single-service package is named after its folder; multi-service packages
        # qualify each service so `nginx` from two packages stays distinguishable.
        slug = f"{package_slug}_{normalise_slug(name)}" if multi else package_slug
        raw_entry = raw_services.get(name) if isinstance(raw_services, dict) else None
        specs.append(
            service_from_mapping(
                slug=slug,
                name=name,
                package=package,
                raw=raw,
                origin=origin,
                source_dir=directory,
                declared_volumes=declared_volumes,
                env_vars=env.entries,
                raw_environment=_as_mapping(
                    raw_entry.get("environment") if isinstance(raw_entry, dict) else None
                ),
            )
        )

    # depends_on refers to compose service names; rewrite to our slugs.
    by_name = {spec.name: spec.slug for spec in specs}
    for spec in specs:
        spec.depends_on = [by_name[dep] for dep in spec.depends_on if dep in by_name]

    return specs
