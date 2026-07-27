"""Recipe definition and YAML loading.

A recipe describes how one kind of runtime is installed into the bundle image, how its
configuration is copied in, which port it listens on and how to change that port, and
what supervisord should run. Supporting a new kind of service means adding a YAML file,
not editing Python — that is the whole point of replacing the old ``bundle.sh``.

Recipes never parse a service's own ``entrypoint.sh``; anything that script needs to
happen is left to run at container start. A recipe may however declare :attr:`post_init`
commands, which run after that script and exist to resolve collisions the script cannot
know about (two nginx services both writing ``conf.d/default.conf``, say).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.model import MountKind

#: Base image families a recipe can install onto.
FAMILIES = ("debian", "alpine")

#: Mechanisms for changing the port a service listens on.
PORT_MECHANISMS = ("nginx_conf", "fpm_pool", "cli_flag", "env_var", "replace", "none")

#: Values allowed in a recipe's ``mount_kinds``. Besides the real kinds, ``skip`` drops
#: a mount outright — used for files the bundle replaces with its own, such as a
#: package's ``supervisord.conf``.
MOUNT_KIND_VALUES = frozenset({k.value for k in MountKind} | {"skip"})


class RecipeError(ValueError):
    """Raised for a recipe file that cannot be loaded."""


@dataclass
class RecipeMatch:
    """How a recipe claims a service. Any populated field that matches counts."""

    image: list[str] = field(default_factory=list)
    """fnmatch globs against the image reference, e.g. ``php:*fpm*``."""

    files: list[str] = field(default_factory=list)
    """Filenames that must exist in the service's source directory."""

    command: list[str] = field(default_factory=list)
    """Substrings looked for in the joined entrypoint + command."""

    service: list[str] = field(default_factory=list)
    """fnmatch globs against the compose service name."""

    package: list[str] = field(default_factory=list)
    """fnmatch globs against the package or project name.

    Often the only thing that distinguishes two services built from the same base image:
    ``node(backend)-nginx`` and ``vue-nginx-vite`` both expose a service plainly called
    ``node``, and only the package says which is which.
    """

    def score(
        self, *, image: str, files: set[str], command: str, service: str, package: str = ""
    ) -> int:
        """Return a match strength; 0 means "does not apply".

        Image matches score highest because an image reference is the most specific
        statement of what a service actually runs. A *mismatching* image vetoes the
        recipe outright — ``php-fpm`` must never claim a Postgres container. An *absent*
        image does not veto, so a service we could not resolve an image for can still be
        matched on its files or its name.
        """
        total = 0
        if self.image and image:
            if any(fnmatch.fnmatch(image, pattern) for pattern in self.image):
                total += 100
            else:
                return 0
        if self.service and any(fnmatch.fnmatch(service, pattern) for pattern in self.service):
            total += 20
        if self.package and any(
            fnmatch.fnmatch(package.lower(), pattern) for pattern in self.package
        ):
            total += 15
        if self.files and any(name in files for name in self.files):
            total += 10
        if self.command and any(token in command for token in self.command):
            total += 5
        return total


@dataclass
class CopyRule:
    """One file or directory to bake into the image."""

    src: str = ""
    """Path relative to the service's source directory."""

    dest: str = ""
    """Destination inside the image. Supports ``{slug}`` and ``{port}``."""

    kind: MountKind = MountKind.CONFIG
    optional: bool = True
    chmod: str = ""
    from_mount: str = ""
    """Take the host side of the compose mount with this target instead of ``src``."""


@dataclass
class PortRule:
    default: int = 0
    mechanism: str = "none"
    file: str = ""
    flag: str = ""
    var: str = ""
    pattern: str = ""
    """For ``replace``: a regex whose first group is the port number."""


@dataclass
class ProgramRule:
    """One ``[program:...]`` the recipe contributes."""

    name: str = ""
    command: str = ""
    directory: str = ""
    priority: int = 50
    stopsignal: str = "TERM"
    stopwaitsecs: int = 10
    startsecs: int = 1
    scalable: bool = True
    """False when extra processes would fight over the same port."""

    critical: bool = True
    environment: dict[str, str] = field(default_factory=dict)


@dataclass
class ReadinessRule:
    type: str = "tcp"
    target: str = "{port}"
    timeout: int = 30


@dataclass
class Recipe:
    name: str
    match: RecipeMatch = field(default_factory=RecipeMatch)
    priority: int = 50
    """Tie-breaker when two recipes score equally; higher wins."""

    bakeable: bool = True
    reason: str = ""
    """Why a non-bakeable recipe cannot be baked; shown to the user."""

    families: list[str] = field(default_factory=lambda: list(FAMILIES))
    install: dict[str, list[str]] = field(default_factory=dict)
    run: dict[str, list[str]] = field(default_factory=dict)
    copy: list[CopyRule] = field(default_factory=list)
    port: PortRule | None = None
    programs: list[ProgramRule] = field(default_factory=list)
    readiness: list[ReadinessRule] = field(default_factory=list)

    post_copy: list[str] = field(default_factory=list)
    """Per-service ``RUN`` lines emitted right after that service's ``COPY`` block.

    Where a copied file has to be adapted to living alongside others — renumbering a
    port, rewriting a baked-in path, adding the user column ``/etc/cron.d`` requires.
    """

    pre_init: list[str] = field(default_factory=list)
    """Commands run in the entrypoint just before the service's own entrypoint.

    Used to stage a per-service file at the fixed path that entrypoint expects, which is
    how several services that each want ``/tmp/nginx.conf.template`` can coexist.
    """

    post_init: list[str] = field(default_factory=list)
    mount_kinds: dict[str, str] = field(default_factory=dict)
    """Mount-target glob -> one of :data:`MOUNT_KIND_VALUES`, overriding heuristics."""

    shared: str = ""
    """Runtime key. Services sharing a key collapse onto one set of programs."""

    shared_install_once: bool = True
    source: Path | None = None
    """File the recipe was loaded from; not part of its identity."""

    def installs_for(self, family: str) -> list[str]:
        return list(self.install.get(family, []))

    def runs_for(self, family: str) -> list[str]:
        return list(self.run.get(family, []))

    def supports(self, family: str) -> bool:
        return family in self.families


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _family_map(value: Any, where: str) -> dict[str, list[str]]:
    if value is None:
        return {}
    if isinstance(value, list):
        # A bare list applies to every family.
        return {family: [str(v) for v in value] for family in FAMILIES}
    if not isinstance(value, dict):
        raise RecipeError(f"{where} must be a list or a mapping of family -> list")
    result: dict[str, list[str]] = {}
    for family, items in value.items():
        if family not in FAMILIES:
            raise RecipeError(f"{where}: unknown base family {family!r}")
        result[str(family)] = _as_str_list(items)
    return result


def _copy_rules(value: Any, where: str) -> list[CopyRule]:
    rules: list[CopyRule] = []
    for index, item in enumerate(value or []):
        if not isinstance(item, dict):
            raise RecipeError(f"{where}[{index}] must be a mapping")
        kind = str(item.get("kind", "config")).lower()
        if kind not in {k.value for k in MountKind}:
            raise RecipeError(f"{where}[{index}].kind: unknown kind {kind!r}")
        dest = str(item.get("dest", ""))
        if not dest:
            raise RecipeError(f"{where}[{index}] needs a dest")
        rules.append(
            CopyRule(
                src=str(item.get("src", "")),
                dest=dest,
                kind=MountKind(kind),
                optional=bool(item.get("optional", True)),
                chmod=str(item.get("chmod", "")),
                from_mount=str(item.get("from_mount", "")),
            )
        )
    return rules


def _port_rule(value: Any, where: str) -> PortRule | None:
    if value is None:
        return None
    if isinstance(value, int):
        return PortRule(default=value)
    if not isinstance(value, dict):
        raise RecipeError(f"{where} must be an integer or a mapping")

    configure = value.get("configure") or {}
    if isinstance(configure, str):
        configure = {"type": configure}
    mechanism = str(configure.get("type", "none"))
    if mechanism not in PORT_MECHANISMS:
        raise RecipeError(
            f"{where}.configure.type: expected one of {', '.join(PORT_MECHANISMS)}, "
            f"got {mechanism!r}"
        )

    return PortRule(
        default=int(value.get("default", 0)),
        mechanism=mechanism,
        file=str(configure.get("file", "")),
        flag=str(configure.get("flag", "")),
        var=str(configure.get("var", "")),
        pattern=str(configure.get("pattern", "")),
    )


def _programs(value: Any, where: str) -> list[ProgramRule]:
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    programs: list[ProgramRule] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RecipeError(f"{where}[{index}] must be a mapping")
        command = str(item.get("command", ""))
        if not command:
            raise RecipeError(f"{where}[{index}] needs a command")
        programs.append(
            ProgramRule(
                name=str(item.get("name", "")),
                command=command,
                directory=str(item.get("directory", "")),
                priority=int(item.get("priority", 50)),
                stopsignal=str(item.get("stopsignal", "TERM")).upper(),
                stopwaitsecs=int(item.get("stopwaitsecs", 10)),
                startsecs=int(item.get("startsecs", 1)),
                scalable=bool(item.get("scalable", True)),
                critical=bool(item.get("critical", True)),
                environment={
                    str(k): str(v) for k, v in (item.get("environment") or {}).items()
                },
            )
        )
    return programs


def _readiness(value: Any, where: str) -> list[ReadinessRule]:
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    rules: list[ReadinessRule] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RecipeError(f"{where}[{index}] must be a mapping")
        kind = str(item.get("type", "tcp"))
        if kind not in ("tcp", "http", "command"):
            raise RecipeError(f"{where}[{index}].type: expected tcp|http|command, got {kind!r}")
        rules.append(
            ReadinessRule(
                type=kind,
                target=str(item.get("target", "{port}")),
                timeout=int(item.get("timeout", 30)),
            )
        )
    return rules


def from_dict(raw: Any, *, source: Path | None = None) -> Recipe:
    where = str(source) if source else "recipe"
    if not isinstance(raw, dict):
        raise RecipeError(f"{where}: expected a mapping at the top level")

    name = str(raw.get("name", "")).strip()
    if not name:
        raise RecipeError(f"{where}: needs a name")

    match_raw = raw.get("match") or {}
    if not isinstance(match_raw, dict):
        raise RecipeError(f"{where}.match must be a mapping")

    families = _as_str_list(raw.get("families") or raw.get("base_family")) or list(FAMILIES)
    for family in families:
        if family not in FAMILIES:
            raise RecipeError(f"{where}.families: unknown base family {family!r}")

    for target, kind in (raw.get("mount_kinds") or {}).items():
        if str(kind) not in MOUNT_KIND_VALUES:
            raise RecipeError(
                f"{where}.mount_kinds[{target}]: expected one of "
                f"{', '.join(sorted(MOUNT_KIND_VALUES))}, got {kind!r}"
            )

    return Recipe(
        name=name,
        match=RecipeMatch(
            image=_as_str_list(match_raw.get("image")),
            files=_as_str_list(match_raw.get("files")),
            command=_as_str_list(match_raw.get("command")),
            service=_as_str_list(match_raw.get("service")),
            package=_as_str_list(match_raw.get("package")),
        ),
        priority=int(raw.get("priority", 50)),
        bakeable=bool(raw.get("bakeable", True)),
        reason=str(raw.get("reason", "")),
        families=families,
        install=_family_map(raw.get("install"), f"{where}.install"),
        run=_family_map(raw.get("run"), f"{where}.run"),
        copy=_copy_rules(raw.get("copy"), f"{where}.copy"),
        port=_port_rule(raw.get("port"), f"{where}.port"),
        programs=_programs(raw.get("supervisor") or raw.get("programs"), f"{where}.supervisor"),
        readiness=_readiness(raw.get("readiness"), f"{where}.readiness"),
        post_copy=_as_str_list(raw.get("post_copy")),
        pre_init=_as_str_list(raw.get("pre_init")),
        post_init=_as_str_list(raw.get("post_init")),
        mount_kinds={str(k): str(v) for k, v in (raw.get("mount_kinds") or {}).items()},
        shared=str(raw.get("shared", "")),
        source=source,
    )


def load_file(path: Path) -> Recipe:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise RecipeError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RecipeError(f"{path} is not valid YAML: {exc}") from exc
    return from_dict(raw, source=path)


def load_dir(directory: Path) -> tuple[list[Recipe], list[str]]:
    """Load every ``*.yml`` in a directory, collecting per-file errors as warnings."""
    recipes: list[Recipe] = []
    warnings: list[str] = []
    if not directory.is_dir():
        return recipes, warnings

    for path in sorted(directory.glob("*.y*ml")):
        try:
            recipes.append(load_file(path))
        except RecipeError as exc:
            warnings.append(str(exc))
    return recipes, warnings
