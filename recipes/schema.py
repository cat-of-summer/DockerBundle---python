"""Recipe definition, merging and loading.

A recipe describes how one kind of runtime is installed into the bundle image, how its
configuration is copied in, which port it listens on and how to change that port, and
what supervisord should run. Supporting a new kind of service means writing YAML, not
editing Python — that is the whole point of replacing the old ``bundle.sh``.

Recipes never parse a service's own ``entrypoint.sh``; anything that script needs to
happen is left to run at container start. A recipe may however declare :attr:`post_init`
commands, which run after that script and exist to resolve collisions the script cannot
know about (two nginx services both writing ``conf.d/default.conf``, say).

Recipes arrive in layers — built-in, then whatever ``recipe_paths:`` names, then the
``recipes:`` written inline in ``docker-bundle.yml`` — and a later layer can either
replace a name outright or, with ``extends:``, build on it. Merging happens on the raw
YAML before anything is parsed, which is what keeps the rule small enough to state in one
line: **a plain key replaces, a ``+key`` appends**.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core import features as features_mod
from core.model import MountKind

#: Base image families a recipe can install onto.
FAMILIES = ("debian", "alpine")

#: Mechanisms for changing the port a service listens on.
PORT_MECHANISMS = ("nginx_conf", "fpm_pool", "cli_flag", "env_var", "replace", "none")

#: What to do with the package's own ``entrypoint.sh``. ``auto`` runs it during the init
#: phase; ``skip`` ignores it, for scripts that never return because they end by exec'ing
#: the server the recipe already starts as a supervisord program.
ENTRYPOINT_MODES = ("auto", "skip")

#: Values allowed in a recipe's ``mount_kinds``. Besides the real kinds, ``skip`` drops
#: a mount outright — used for files the bundle replaces with its own, such as a
#: package's ``supervisord.conf``.
MOUNT_KIND_VALUES = frozenset({k.value for k in MountKind} | {"skip"})


class RecipeError(ValueError):
    """Raised for a recipe that cannot be loaded or merged."""


def _when(raw: Any, where: str) -> list[str]:
    try:
        return features_mod.normalise(raw, where=where)
    except features_mod.FeatureError as exc:
        raise RecipeError(str(exc)) from exc


def _holds(when: list[str], features: dict[str, bool], where: str) -> bool:
    try:
        return features_mod.evaluate(when, features, where=where)
    except features_mod.FeatureError as exc:
        raise RecipeError(str(exc)) from exc


@dataclass
class Step:
    """One shell command a recipe contributes, possibly behind a feature flag."""

    cmd: str
    when: list[str] = field(default_factory=list)


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

    path: list[str] = field(default_factory=list)
    """fnmatch globs against the service's source directory, as a posix path.

    For layouts where neither the image nor the service name is distinctive but the tree
    is: ``*/services/nginx`` claims the nginx leg of a package that splits per service.
    """

    env: list[str] = field(default_factory=list)
    """Environment keys the service declares, e.g. ``POSTGRES_PASSWORD``.

    The last resort for a service built from a private image nobody wrote a recipe for:
    what it is often shows in what it asks to be configured with.
    """

    def score(
        self,
        *,
        image: str,
        files: set[str],
        command: str,
        service: str,
        package: str = "",
        path: str = "",
        env: set[str] | None = None,
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
        if self.path and path and any(fnmatch.fnmatch(path, pattern) for pattern in self.path):
            total += 10
        if self.env and env and any(name in env for name in self.env):
            total += 5
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

    from_image: str = ""
    """Take ``src`` from inside this image rather than from the build context.

    Lets a recipe lift just the part of an upstream image it needs — a language runtime,
    a jar — instead of falling back to importing that image's whole filesystem.
    """

    when: list[str] = field(default_factory=list)


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
    when: list[str] = field(default_factory=list)


@dataclass
class ReadinessRule:
    type: str = "tcp"
    target: str = "{port}"
    timeout: int = 30
    when: list[str] = field(default_factory=list)


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
    run: dict[str, list[Step]] = field(default_factory=dict)
    copy: list[CopyRule] = field(default_factory=list)
    port: PortRule | None = None
    programs: list[ProgramRule] = field(default_factory=list)
    readiness: list[ReadinessRule] = field(default_factory=list)

    entrypoint: str = "auto"
    """Whether the service's own ``entrypoint.sh`` runs during the init phase.

    ``skip`` is for packages whose entrypoint ends in ``exec <server>``: the init phase
    calls it and waits for it to return, so such a script would hang start-up forever.
    A recipe that sets this is expected to start the service itself via ``supervisor:``.
    """

    post_copy: list[Step] = field(default_factory=list)
    """Per-service ``RUN`` lines emitted right after that service's ``COPY`` block.

    Where a copied file has to be adapted to living alongside others — renumbering a
    port, rewriting a baked-in path, adding the user column ``/etc/cron.d`` requires.
    """

    pre_init: list[Step] = field(default_factory=list)
    """Commands run in the entrypoint just before the service's own entrypoint.

    Used to stage a per-service file at the fixed path that entrypoint expects, which is
    how several services that each want ``/tmp/nginx.conf.template`` can coexist.
    """

    post_init: list[Step] = field(default_factory=list)
    mount_kinds: dict[str, str] = field(default_factory=dict)
    """Mount-target glob -> one of :data:`MOUNT_KIND_VALUES`, overriding heuristics."""

    shared: str = ""
    """Runtime key. Services sharing a key collapse onto one set of programs."""

    shared_install_once: bool = True
    source: str = ""
    """Where the recipe was loaded from; not part of its identity."""

    extends: str = ""
    """Recipe this one was built on, for messages. Already applied by the time you see it."""

    def installs_for(self, family: str) -> list[str]:
        return list(self.install.get(family, []))

    def supports(self, family: str) -> bool:
        return family in self.families

    # -- feature-filtered views -------------------------------------------
    #
    # Every rule a recipe carries can be held behind a ``when:``, so nothing reads these
    # lists directly. Filtering here rather than at each call site is what keeps the
    # planner from having to know that features exist at all.

    def runs_for(self, family: str, features: dict[str, bool]) -> list[str]:
        return _active(self.run.get(family, []), features, f"{self.name}.run.{family}")

    def post_copy_for(self, features: dict[str, bool]) -> list[str]:
        return _active(self.post_copy, features, f"{self.name}.post_copy")

    def pre_init_for(self, features: dict[str, bool]) -> list[str]:
        return _active(self.pre_init, features, f"{self.name}.pre_init")

    def post_init_for(self, features: dict[str, bool]) -> list[str]:
        return _active(self.post_init, features, f"{self.name}.post_init")

    def copies_for(self, features: dict[str, bool]) -> list[CopyRule]:
        return [
            rule
            for rule in self.copy
            if _holds(rule.when, features, f"{self.name}.copy")
        ]

    def programs_for(self, features: dict[str, bool]) -> list[ProgramRule]:
        return [
            rule
            for rule in self.programs
            if _holds(rule.when, features, f"{self.name}.supervisor")
        ]

    def readiness_for(self, features: dict[str, bool]) -> list[ReadinessRule]:
        return [
            rule
            for rule in self.readiness
            if _holds(rule.when, features, f"{self.name}.readiness")
        ]


def _active(steps: list[Step], features: dict[str, bool], where: str) -> list[str]:
    return [step.cmd for step in steps if _holds(step.when, features, where)]


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------


@dataclass
class RawRecipe:
    """A recipe body as written, before parsing. What ``extends:`` operates on."""

    name: str
    body: dict[str, Any]
    origin: str
    """File it came from, or the configuration file for an inline one."""

    @property
    def extends(self) -> str:
        return str(self.body.get("extends", "") or "")


def _append(parent: Any, child: Any, where: str) -> Any:
    """Combine a ``+key`` value with the one it is appending to.

    Lists concatenate and mappings merge key by key, recursing where both sides are
    containers so that ``+install: {debian: [iproute2]}`` reaches the list inside. A leaf
    that exists on both sides is simply overwritten: ``+mount_kinds`` adding a glob the
    parent already classifies means the child has a better answer for it.
    """
    if parent is None:
        return child
    if isinstance(parent, list) and isinstance(child, list):
        return [*parent, *child]
    if isinstance(parent, dict) and isinstance(child, dict):
        merged = dict(parent)
        for key, value in child.items():
            existing = merged.get(key)
            if isinstance(existing, (list, dict)) and isinstance(value, (list, dict)):
                merged[key] = _append(existing, value, f"{where}.{key}")
            else:
                merged[key] = value
        return merged
    raise RecipeError(
        f"+{where}: cannot append {type(child).__name__} to {type(parent).__name__}; "
        f"drop the + to replace it instead"
    )


def merge_bodies(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """Apply a child recipe body on top of the one it extends.

    A plain key replaces what the parent said — including with an empty list, which is
    how a child drops steps it does not want. A ``+key`` appends to it.
    """
    result = {key: value for key, value in parent.items() if key != "extends"}
    for key, value in child.items():
        if key == "extends":
            continue
        if key.startswith("+"):
            base = key[1:]
            if not base:
                raise RecipeError("'+' is not a key on its own")
            result[base] = _append(result.get(base), value, base)
        else:
            result[key] = value
    return result


def build_table(layers: list[list[RawRecipe]]) -> tuple[dict[str, Recipe], list[str]]:
    """Resolve ``extends:`` across layers and parse the result.

    Layers are given lowest-priority first: built-in, then ``recipe_paths:``, then the
    inline ``recipes:``. Within one layer the order recipes were read in does not matter,
    because an entry waits until whatever it extends has been resolved.
    """
    table: dict[str, RawRecipe] = {}
    warnings: list[str] = []

    for layer in layers:
        pending = list(layer)
        while pending:
            progressed = False
            deferred: list[RawRecipe] = []
            same_layer = {entry.name for entry in pending}

            for entry in pending:
                parent_name = entry.extends
                if parent_name and parent_name != entry.name and parent_name in same_layer:
                    # Its parent is still waiting its turn in this same layer.
                    deferred.append(entry)
                    continue
                table[entry.name] = _resolve_one(entry, table)
                progressed = True

            if not progressed:
                chain = ", ".join(sorted(entry.name for entry in deferred))
                raise RecipeError(f"extends: forms a cycle between {chain}")
            pending = deferred

    recipes: dict[str, Recipe] = {}
    for name, entry in table.items():
        try:
            recipes[name] = from_dict(entry.body, source=entry.origin)
        except RecipeError as exc:
            warnings.append(str(exc))
    return recipes, warnings


def _resolve_one(entry: RawRecipe, table: dict[str, RawRecipe]) -> RawRecipe:
    parent_name = entry.extends
    if not parent_name:
        return entry

    parent = table.get(parent_name)
    if parent is None:
        known = ", ".join(sorted(table)) or "none"
        raise RecipeError(
            f"{entry.origin}: recipe {entry.name!r} extends {parent_name!r}, which is not "
            f"defined in any earlier layer (known: {known})"
        )
    if parent is entry:
        raise RecipeError(f"{entry.origin}: recipe {entry.name!r} extends itself")

    body = merge_bodies(parent.body, entry.body)
    body["name"] = entry.name
    return RawRecipe(name=entry.name, body=body, origin=entry.origin)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _steps(value: Any, where: str) -> list[Step]:
    """Read a list of shell commands, each optionally guarded by ``when:``."""
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        raise RecipeError(f"{where} must be a command or a list of commands")

    steps: list[Step] = []
    for index, item in enumerate(value):
        if isinstance(item, dict):
            unknown = set(item) - {"cmd", "when"}
            if unknown:
                raise RecipeError(
                    f"{where}[{index}]: unknown key(s) {', '.join(sorted(unknown))}; "
                    f"expected cmd or when"
                )
            command = str(item.get("cmd", ""))
            if not command:
                raise RecipeError(f"{where}[{index}] needs a cmd")
            steps.append(Step(cmd=command, when=_when(item.get("when"), f"{where}[{index}]")))
        else:
            steps.append(Step(cmd=str(item)))
    return steps


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


def _family_steps(value: Any, where: str) -> dict[str, list[Step]]:
    if value is None:
        return {}
    if isinstance(value, list):
        shared = _steps(value, where)
        return {family: list(shared) for family in FAMILIES}
    if not isinstance(value, dict):
        raise RecipeError(f"{where} must be a list or a mapping of family -> list")
    result: dict[str, list[Step]] = {}
    for family, items in value.items():
        if family not in FAMILIES:
            raise RecipeError(f"{where}: unknown base family {family!r}")
        result[str(family)] = _steps(items, f"{where}.{family}")
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
        from_image = str(item.get("from_image", ""))
        if from_image and not item.get("src"):
            raise RecipeError(f"{where}[{index}]: from_image needs a src path inside that image")
        rules.append(
            CopyRule(
                src=str(item.get("src", "")),
                dest=dest,
                kind=MountKind(kind),
                optional=bool(item.get("optional", True)),
                chmod=str(item.get("chmod", "")),
                from_mount=str(item.get("from_mount", "")),
                from_image=from_image,
                when=_when(item.get("when"), f"{where}[{index}]"),
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
                when=_when(item.get("when"), f"{where}[{index}]"),
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
                when=_when(item.get("when"), f"{where}[{index}]"),
            )
        )
    return rules


def from_dict(raw: Any, *, source: str = "") -> Recipe:
    where = source or "recipe"
    if not isinstance(raw, dict):
        raise RecipeError(f"{where}: expected a mapping at the top level")

    name = str(raw.get("name", "")).strip()
    if not name:
        raise RecipeError(f"{where}: needs a name")

    leftover = [key for key in raw if key.startswith("+")]
    if leftover:
        raise RecipeError(
            f"{where}: {', '.join(sorted(leftover))} only mean something with extends:; "
            f"without a recipe to append to there is nothing to add on"
        )

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

    entrypoint = str(raw.get("entrypoint", "auto")).lower()
    if entrypoint not in ENTRYPOINT_MODES:
        raise RecipeError(
            f"{where}.entrypoint: expected one of {', '.join(ENTRYPOINT_MODES)}, "
            f"got {raw.get('entrypoint')!r}"
        )

    return Recipe(
        name=name,
        match=RecipeMatch(
            image=_as_str_list(match_raw.get("image")),
            files=_as_str_list(match_raw.get("files")),
            command=_as_str_list(match_raw.get("command")),
            service=_as_str_list(match_raw.get("service")),
            package=_as_str_list(match_raw.get("package")),
            path=_as_str_list(match_raw.get("path")),
            env=_as_str_list(match_raw.get("env")),
        ),
        priority=int(raw.get("priority", 50)),
        bakeable=bool(raw.get("bakeable", True)),
        reason=str(raw.get("reason", "")),
        families=families,
        install=_family_map(raw.get("install"), f"{where}.install"),
        run=_family_steps(raw.get("run"), f"{where}.run"),
        copy=_copy_rules(raw.get("copy"), f"{where}.copy"),
        port=_port_rule(raw.get("port"), f"{where}.port"),
        programs=_programs(raw.get("supervisor") or raw.get("programs"), f"{where}.supervisor"),
        readiness=_readiness(raw.get("readiness"), f"{where}.readiness"),
        entrypoint=entrypoint,
        post_copy=_steps(raw.get("post_copy"), f"{where}.post_copy"),
        pre_init=_steps(raw.get("pre_init"), f"{where}.pre_init"),
        post_init=_steps(raw.get("post_init"), f"{where}.post_init"),
        mount_kinds={str(k): str(v) for k, v in (raw.get("mount_kinds") or {}).items()},
        shared=str(raw.get("shared", "")),
        source=source,
        extends=str(raw.get("extends", "") or ""),
    )


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def read_file(path: Path) -> RawRecipe:
    """Read one recipe file into its raw body."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise RecipeError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RecipeError(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise RecipeError(f"{path}: expected a mapping at the top level")
    name = str(raw.get("name", "")).strip()
    if not name:
        raise RecipeError(f"{path}: needs a name")
    return RawRecipe(name=name, body=raw, origin=str(path))


def read_path(target: Path) -> tuple[list[RawRecipe], list[str]]:
    """Read a recipe file, or every ``*.yml`` in a directory.

    Per-file errors are collected as warnings rather than raised: one unreadable recipe
    should not stop a generation that never uses it.
    """
    entries: list[RawRecipe] = []
    warnings: list[str] = []

    if target.is_file():
        candidates = [target]
    elif target.is_dir():
        candidates = sorted(target.glob("*.y*ml"))
    else:
        return entries, [f"recipe_paths: {target} does not exist"]

    for path in candidates:
        try:
            entries.append(read_file(path))
        except RecipeError as exc:
            warnings.append(str(exc))
    return entries, warnings


def read_inline(mapping: dict[str, Any], origin: str) -> tuple[list[RawRecipe], list[str]]:
    """Read the ``recipes:`` section of ``docker-bundle.yml``.

    The mapping key names the recipe, so a body does not have to repeat it; one that
    does anyway had better agree.
    """
    entries: list[RawRecipe] = []
    warnings: list[str] = []

    for name, body in mapping.items():
        key = str(name)
        if not isinstance(body, dict):
            warnings.append(f"{origin}: recipes.{key} must be a mapping")
            continue
        declared = str(body.get("name", "") or "").strip()
        if declared and declared != key:
            warnings.append(
                f"{origin}: recipes.{key} declares name: {declared!r}; "
                f"the key is what names a recipe, so {key!r} is used"
            )
        merged = dict(body)
        merged["name"] = key
        entries.append(RawRecipe(name=key, body=merged, origin=f"{origin} recipes.{key}"))
    return entries, warnings
