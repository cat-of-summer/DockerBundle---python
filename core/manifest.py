"""``docker-bundle.yml`` — the one file that describes a bundle.

Everything generation needs lives here: where to look for services, which of them go into
the image, the recipes that say how, how colliding environment keys are renamed, and the
boolean switches that select between shapes of the same stand. The Textual wizard only
edits this file, and ``dockerbundle generate --yes`` reads nothing else.

The file is meant to be committed, so it pins every decision that would otherwise drift
between runs. It is deliberately *not* a second copy of the compose files: it says how to
resolve what they disagree about, and stays silent where they already agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from core import features as features_mod
from core.model import PUBLISH_NONE, MountMode, ServiceMode
from core.version import MANIFEST_VERSION

DEFAULT_BASES: dict[str, str] = {
    "cpu": "debian:bookworm-slim",
    "cuda": "nvidia/cuda:12.4.0-base-ubuntu22.04",
}

#: Keys that stay shared across all services instead of being renamed. These are
#: infrastructure-wide in every package we have seen.
DEFAULT_GLOBALS: tuple[str, ...] = (
    "NETWORK",
    "INSTANCE",
    "TZ",
    "TRAEFIK_ENABLE",
    "TRAEFIK_DOMAIN",
    "TRAEFIK_ENTRYPOINT",
    "ACME_EMAIL",
)

#: Prefixes a short-form ``env:`` rule may carry, besides the bare word ``prefix``.
ENV_RULE_PREFIXES = ("keep:", "value:", "local:")


class ManifestError(ValueError):
    """Raised for a configuration that cannot be acted on."""


def _when(raw: Any, where: str) -> list[str]:
    try:
        return features_mod.normalise(raw, where=where)
    except features_mod.FeatureError as exc:
        raise ManifestError(str(exc)) from exc


#: A bind's name becomes an environment variable, so it has to survive being upper-cased.
_BIND_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

#: Names ``dist/`` already uses. A bind shipped onto one of them would be overwritten by the
#: generator, or overwrite what the generator wrote.
RESERVED_DIST_NAMES = frozenset({
    "context", "Dockerfile", "docker-compose.yml", ".env", ".env.example", ".dockerignore",
    "entrypoint.sh", "healthcheck.sh", "supervisord.conf", "docker-bundle.lock.yml",
})


@dataclass
class Bind:
    """A part of the container the deployment keeps on its own disk, next to the compose file.

    A named volume hides its content inside Docker; a bind is for what a person is meant to
    open and edit on the server — a dictionary, a list of hosts, a template. ``ship`` is
    what the generator puts into ``dist/<host>`` as the starting content, so the release
    carries it and an unpacked ``dist/`` runs as is.
    """

    path: str
    """Absolute path inside the bundle container."""

    host: str = ""
    """Path next to the generated compose file. Defaults to ``./<name>``."""

    ship: str = ""
    """File or directory, relative to ``docker-bundle.yml``, copied into ``dist/<host>``."""

    when: list[str] = field(default_factory=list)

    def to_dict(self, name: str) -> Any:
        data: dict[str, Any] = {"path": self.path}
        if self.host and self.host != default_bind_host(name):
            data["host"] = self.host
        if self.ship:
            data["ship"] = self.ship
        if self.when:
            data["when"] = list(self.when)
        return data

    @classmethod
    def from_dict(cls, name: str, raw: Any) -> Bind:
        where = f"binds.{name}"
        if not _BIND_NAME.match(name):
            raise ManifestError(
                f"{where}: the name becomes an environment variable; use lowercase "
                f"letters, digits and _"
            )
        if isinstance(raw, str):
            raw = {"path": raw}
        if not isinstance(raw, dict):
            raise ManifestError(f"{where}: expected a path or a mapping with path:")
        unknown = set(raw) - {"path", "host", "ship", "when"}
        if unknown:
            raise ManifestError(
                f"{where}: unknown key(s) {', '.join(sorted(unknown))}; "
                f"expected path, host, ship or when"
            )
        path = str(raw.get("path") or "")
        if not path.startswith("/"):
            raise ManifestError(
                f"{where}: path must be absolute inside the container, got {path!r}"
            )
        host = str(raw.get("host") or default_bind_host(name))
        relative = PurePosixPath(host.replace("\\", "/"))
        parts = [part for part in relative.parts if part != "."]
        if relative.is_absolute() or not parts or ".." in parts:
            raise ManifestError(
                f"{where}: host must be a path inside the deployment directory, "
                f"like ./{name}; got {host!r}"
            )
        if parts[0] in RESERVED_DIST_NAMES:
            raise ManifestError(f"{where}: host {host!r} is a name the generator writes itself")
        return cls(
            path=path.rstrip("/") or "/",
            host="./" + "/".join(parts),
            ship=str(raw.get("ship") or ""),
            when=_when(raw.get("when"), where),
        )


def default_bind_host(name: str) -> str:
    return f"./{name}"


@dataclass
class SourceRef:
    """One place to look for candidate services."""

    type: str  # catalog | compose | container | image
    path: str = ""
    ref: str = ""
    name: str = ""

    id: str = ""
    """Label used in messages about this source. Defaults to its path or reference."""

    prefix: str = ""
    """Namespace prepended to every slug this source produces.

    The way to settle a slug collision: five of the seven stands we build from define a
    service plainly called ``nginx``, and inside one bundle those have to stay apart.
    """

    when: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.id or self.path or self.ref or self.name or self.type

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        for key in ("id", "path", "ref", "name", "prefix"):
            value = getattr(self, key)
            if value:
                data[key] = value
        if self.when:
            data["when"] = list(self.when)
        return data

    @classmethod
    def from_dict(cls, raw: Any, *, index: int = 0) -> SourceRef:
        where = f"sources[{index}]"
        if isinstance(raw, str):
            return cls(type="catalog", path=raw)
        if not isinstance(raw, dict):
            raise ManifestError(
                f"{where} must be a mapping or a path string, got {type(raw).__name__}"
            )
        kind = raw.get("type")
        if kind not in ("catalog", "compose", "container", "image"):
            raise ManifestError(f"{where}: unknown source type {kind!r}")
        return cls(
            type=kind,
            path=str(raw.get("path", "")),
            ref=str(raw.get("ref", "")),
            name=str(raw.get("name", "")),
            id=str(raw.get("id", "")),
            prefix=str(raw.get("prefix", "")),
            when=_when(raw.get("when"), where),
        )


def _service_mode(raw: Any, slug: str) -> ServiceMode | None:
    """Read ``mode:``, coping with YAML's reading of the bare word ``off``.

    ``mode: off`` is the natural way to write it and YAML 1.1 hands us ``False`` for it,
    so the value has to be recognised before it is stringified. ``on`` arrives as ``True``
    and is refused: there is no single opposite of "off" here, since a service can come
    back as baked or as a sidecar and only the author knows which.
    """
    if raw is None:
        return None
    if raw is False:
        return ServiceMode.OFF
    if raw is True:
        raise ManifestError(
            f"services.{slug}.mode: `on` is not a mode; write bake, sidecar or external"
        )
    text = str(raw).lower()
    try:
        return ServiceMode(text)
    except ValueError:
        raise ManifestError(
            f"services.{slug}.mode: expected "
            f"{' | '.join(m.value for m in ServiceMode)}, got {raw!r}"
        ) from None


@dataclass
class ServiceEntry:
    """Per-service decisions. The key in ``services:`` is the pinned slug."""

    slug: str
    package: str = ""
    service: str = ""

    mode: ServiceMode | None = None
    """What becomes of this service. ``None`` means "whatever its recipe says"."""

    when: list[str] = field(default_factory=list)

    recipe: str = ""
    """Force a specific recipe instead of matching."""

    replicas: int = 1
    ports: dict[int, int] = field(default_factory=dict)
    """Original container port -> port assigned inside the bundle."""

    publish: dict[int, str] = field(default_factory=dict)
    """Original container port -> host publish spec (``127.0.0.1:8081``), or ``none``."""

    mounts: dict[str, str] = field(default_factory=dict)
    """Mount target -> ``copy`` | ``volume`` | ``skip``."""

    env_prefix: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.package:
            data["package"] = self.package
        if self.service:
            data["service"] = self.service
        if self.mode is not None:
            data["mode"] = self.mode.value
        if self.when:
            data["when"] = list(self.when)
        if self.recipe:
            data["recipe"] = self.recipe
        if self.replicas != 1:
            data["replicas"] = self.replicas
        if self.ports:
            data["ports"] = {int(k): int(v) for k, v in sorted(self.ports.items())}
        if self.publish:
            data["publish"] = {int(k): v for k, v in sorted(self.publish.items())}
        if self.mounts:
            data["mounts"] = dict(sorted(self.mounts.items()))
        if self.env_prefix:
            data["env_prefix"] = self.env_prefix
        return data

    @classmethod
    def from_dict(cls, slug: str, raw: Any) -> ServiceEntry:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ManifestError(f"services.{slug} must be a mapping")

        mounts = {}
        for target, mode in (raw.get("mounts") or {}).items():
            text = str(mode).lower()
            if text not in {m.value for m in MountMode}:
                raise ManifestError(
                    f"services.{slug}.mounts[{target}]: expected copy|volume|skip, got {mode!r}"
                )
            mounts[str(target)] = text

        replicas = raw.get("replicas", 1)
        if not isinstance(replicas, int) or isinstance(replicas, bool) or replicas < 1:
            raise ManifestError(f"services.{slug}.replicas must be a positive integer")

        return cls(
            slug=slug,
            package=str(raw.get("package", "")),
            service=str(raw.get("service", "")),
            mode=_service_mode(raw.get("mode"), slug),
            when=_when(raw.get("when"), f"services.{slug}"),
            recipe=str(raw.get("recipe", "")),
            replicas=replicas,
            ports={int(k): int(v) for k, v in (raw.get("ports") or {}).items()},
            publish=_publish(raw.get("publish"), slug),
            mounts=mounts,
            env_prefix=str(raw.get("env_prefix", "")),
        )


def _publish(raw: Any, slug: str) -> dict[int, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ManifestError(f"services.{slug}.publish must be a mapping of port -> spec")
    result: dict[int, str] = {}
    for port, spec in raw.items():
        try:
            original = int(port)
        except (TypeError, ValueError) as exc:
            raise ManifestError(f"services.{slug}.publish: {port!r} is not a port") from exc
        # YAML reads a bare `no`/`false` as a boolean; take it to mean what `none` says.
        text = PUBLISH_NONE if spec is False or spec is None else str(spec).strip()
        if not text:
            raise ManifestError(
                f"services.{slug}.publish.{original}: expected a host spec such as "
                f"127.0.0.1:8081, or {PUBLISH_NONE}"
            )
        result[original] = text
    return result


def _env_rule_text(text: str, where: str) -> str:
    if text != "prefix" and not text.startswith(ENV_RULE_PREFIXES):
        raise ManifestError(
            f"{where}: expected prefix|keep:<slug>|value:<literal>|local:<slug>, got {text!r}"
        )
    for marker in ("keep:", "local:"):
        if text.startswith(marker) and not text[len(marker) :].strip():
            raise ManifestError(f"{where}: {marker} needs a service slug after it")
    return text


@dataclass
class EnvRule:
    """What to do with one environment key that several services define."""

    rule: str = ""
    """``prefix`` | ``keep:<slug>`` | ``value:<literal>`` | ``local:<slug>``, or empty."""

    per_service: dict[str, str] = field(default_factory=dict)
    """Slug -> the name this service's value takes in the merged ``.env``.

    The process still sees the original name: supervisord and the init phase map it back.
    This is how two stands that both read ``TRAEFIK_DOMAIN`` get ``TRAEFIK_DOMAIN_APP``
    and ``TRAEFIK_DOMAIN_VNU`` in one deployment ``.env`` without either of them knowing.
    """

    when: list[str] = field(default_factory=list)

    def to_dict(self) -> Any:
        if self.rule and not self.per_service and not self.when:
            return self.rule
        data: dict[str, Any] = {}
        if self.rule:
            data["rule"] = self.rule
        if self.per_service:
            data["per_service"] = dict(sorted(self.per_service.items()))
        if self.when:
            data["when"] = list(self.when)
        return data

    @classmethod
    def from_dict(cls, key: str, raw: Any) -> EnvRule:
        where = f"env.{key}"
        if isinstance(raw, str):
            return cls(rule=_env_rule_text(raw, where))
        if not isinstance(raw, dict):
            raise ManifestError(
                f"{where} must be a rule string or a mapping with rule/per_service/when"
            )

        unknown = set(raw) - {"rule", "per_service", "when"}
        if unknown:
            raise ManifestError(
                f"{where}: unknown key(s) {', '.join(sorted(unknown))}; "
                f"expected rule, per_service or when"
            )

        per_service_raw = raw.get("per_service") or {}
        if not isinstance(per_service_raw, dict):
            raise ManifestError(f"{where}.per_service must be a mapping of slug -> name")

        per_service: dict[str, str] = {}
        for slug, new_name in per_service_raw.items():
            text = str(new_name)
            if not text:
                raise ManifestError(f"{where}.per_service.{slug}: needs a name")
            per_service[str(slug)] = text

        taken: dict[str, str] = {}
        for slug, new_name in sorted(per_service.items()):
            if new_name in taken:
                raise ManifestError(
                    f"{where}.per_service: {taken[new_name]} and {slug} both map to "
                    f"{new_name!r}; one key cannot hold two services' values"
                )
            taken[new_name] = slug

        rule = raw.get("rule")
        return cls(
            rule=_env_rule_text(str(rule), where) if rule is not None else "",
            per_service=per_service,
            when=_when(raw.get("when"), where),
        )


@dataclass
class Conditional:
    """A value that only applies when its ``when:`` holds. Used by volumes and labels."""

    value: str
    when: list[str] = field(default_factory=list)

    def to_dict(self, *, value_key: str) -> Any:
        if not self.when:
            return self.value
        return {value_key: self.value, "when": list(self.when)}

    @classmethod
    def from_dict(cls, raw: Any, *, where: str, value_key: str) -> Conditional:
        if isinstance(raw, dict):
            unknown = set(raw) - {value_key, "when"}
            if unknown:
                raise ManifestError(
                    f"{where}: unknown key(s) {', '.join(sorted(unknown))}; "
                    f"expected {value_key} or when"
                )
            if value_key not in raw:
                raise ManifestError(f"{where}: needs a {value_key}")
            return cls(value=str(raw[value_key]), when=_when(raw.get("when"), where))
        return cls(value=str(raw))


#: Keys that used to mean something and now have a replacement. Naming them explicitly
#: beats "unknown key": the file otherwise loads, generates a different bundle, and says
#: nothing about the instruction it ignored.
_RETIRED = {
    "env_conflicts": "env: (`env_conflicts: {K: prefix}` becomes `env: {K: prefix}`)",
}


def _refuse_retired_keys(raw: dict[str, Any], name: str) -> None:
    for key, replacement in _RETIRED.items():
        if key in raw:
            raise ManifestError(f"{name}: {key}: has been replaced by {replacement}")

    services = raw.get("services")
    if isinstance(services, dict):
        for slug, entry in services.items():
            if not isinstance(entry, dict):
                continue
            for key in ("enabled", "bakeable"):
                if key in entry:
                    raise ManifestError(
                        f"{name}: services.{slug}.{key}: has been replaced by mode: "
                        f"(bake | sidecar | external | off)"
                    )


def _check_bind_paths(binds: dict[str, Bind], volumes: dict[str, Conditional]) -> None:
    """Refuse two mounts of one container path, or one nested inside another.

    Docker accepts both and the later mount silently wins, so a bind laid over a volume
    would make the volume's data vanish from the container without an error anywhere.
    """
    taken = [(f"volumes.{name}", item.value.rstrip("/") or "/") for name, item in volumes.items()]
    taken += [(f"binds.{name}", bind.path) for name, bind in binds.items()]
    hosts: dict[str, str] = {}
    for index, (where, path) in enumerate(taken):
        for other_where, other in taken[index + 1:]:
            a, b = PurePosixPath(path), PurePosixPath(other)
            if a == b or a in b.parents or b in a.parents:
                raise ManifestError(f"{other_where}: {other} overlaps {where} at {path}")
    for name, bind in binds.items():
        if bind.host in hosts:
            raise ManifestError(
                f"binds.{name}: host {bind.host} is already binds.{hosts[bind.host]}"
            )
        hosts[bind.host] = name


@dataclass
class Manifest:
    name: str = "bundle"
    version: int = MANIFEST_VERSION

    features: dict[str, bool] = field(default_factory=dict)
    """Build-time switches. Overridden per run by ``--enable`` / ``--disable``."""

    sources: list[SourceRef] = field(default_factory=list)

    recipe_paths: list[str] = field(default_factory=list)
    """Extra files or directories of recipes, resolved against this file's directory.

    Optional: recipes are normally written inline under :attr:`recipes`. This exists for a
    recipe long enough that keeping it in its own file reads better, and for a set shared
    between several stands.
    """

    recipes: dict[str, Any] = field(default_factory=dict)
    """Recipes written inline, name -> body. Parsed by :mod:`recipes.schema`."""

    services: dict[str, ServiceEntry] = field(default_factory=dict)
    output: str = "dist"
    image: str = ""
    """Image reference the generated compose defaults to, e.g. ``ghcr.io/acme/stand:latest``.

    ``generate --image`` overrides it. Keeping it here is what stops a plain regeneration
    from silently reverting the published reference to ``<name>:latest``.
    """

    variants: list[str] = field(default_factory=lambda: ["cpu"])
    base: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_BASES))
    network: str = "network"
    network_external: bool = True

    volumes: dict[str, Conditional] = field(default_factory=dict)
    """Extra named volumes: volume name -> absolute path inside the bundle container.

    Mount classification can only speak about paths a source compose file declared. State
    that lives *inside* an otherwise baked directory — generated reports and visual
    baselines under a code tree — has no mount of its own to classify, and without an
    entry here it would be lost on the next ``docker pull``.
    """

    binds: dict[str, Bind] = field(default_factory=dict)
    """Host paths mounted into the bundle container: name -> :class:`Bind`.

    For what the deployment edits by hand. Everything else that must survive the image
    belongs in :attr:`volumes`.
    """

    labels: dict[str, Conditional] = field(default_factory=dict)
    """Labels forced onto the bundle container, overriding what the services declared.

    A label describes the whole container, so two services asking for different values of
    one key cannot both be satisfied. Generation keeps the first and says so; this is
    where the author overrules that.
    """

    globals: list[str] = field(default_factory=lambda: list(DEFAULT_GLOBALS))
    env: dict[str, EnvRule] = field(default_factory=dict)
    port_range: tuple[int, int] = (20000, 20999)
    path: Path | None = None
    """Where this configuration was loaded from; not serialised."""

    def __post_init__(self) -> None:
        # Built by hand as often as it is loaded from YAML — by the wizard, by `init`, by
        # tests — and at those call sites a volume is a path and a label is a value.
        # Accepting the plain form here keeps `when:` from leaking into every one of them.
        self.volumes = {
            name: item if isinstance(item, Conditional) else Conditional(value=str(item))
            for name, item in self.volumes.items()
        }
        self.labels = {
            key: item if isinstance(item, Conditional) else Conditional(value=str(item))
            for key, item in self.labels.items()
        }
        self.env = {
            key: rule if isinstance(rule, EnvRule) else EnvRule.from_dict(key, rule)
            for key, rule in self.env.items()
        }

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": self.version,
            "name": self.name,
            "output": self.output,
            "image": self.image,
            "variants": list(self.variants),
            "base": dict(self.base),
            "network": {"name": self.network, "external": self.network_external},
            "port_range": list(self.port_range),
            "globals": list(self.globals),
        }
        if self.features:
            data["features"] = dict(sorted(self.features.items()))
        data["sources"] = [source.to_dict() for source in self.sources]
        if self.recipe_paths:
            data["recipe_paths"] = list(self.recipe_paths)
        if self.recipes:
            data["recipes"] = dict(self.recipes)
        if self.volumes:
            data["volumes"] = {
                name: item.to_dict(value_key="path")
                for name, item in sorted(self.volumes.items())
            }
        if self.binds:
            data["binds"] = {
                name: bind.to_dict(name) for name, bind in sorted(self.binds.items())
            }
        if self.labels:
            data["labels"] = {
                key: item.to_dict(value_key="value")
                for key, item in sorted(self.labels.items())
            }
        data["env"] = {key: rule.to_dict() for key, rule in sorted(self.env.items())}
        data["services"] = {slug: entry.to_dict() for slug, entry in self.services.items()}
        return data

    def dump(self) -> str:
        return yaml.safe_dump(
            self.to_dict(), sort_keys=False, allow_unicode=True, default_flow_style=False
        )

    def save(self, path: Path | None = None) -> Path:
        target = path or self.path
        if target is None:
            raise ManifestError("no path to save the configuration to")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.dump(), encoding="utf-8", newline="\n")
        self.path = target
        return target

    # -- deserialisation --------------------------------------------------

    @classmethod
    def from_dict(cls, raw: Any, *, path: Path | None = None) -> Manifest:
        name = path.name if path else "docker-bundle.yml"
        if not isinstance(raw, dict):
            raise ManifestError(f"{name} must contain a mapping at the top level")

        version = raw.get("version", MANIFEST_VERSION)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ManifestError("version must be an integer")
        if version > MANIFEST_VERSION:
            raise ManifestError(
                f"{name} declares version {version} but this build only understands "
                f"{MANIFEST_VERSION}; upgrade dockerbundle"
            )
        _refuse_retired_keys(raw, name)

        try:
            features = features_mod.parse_features(raw.get("features"))
        except features_mod.FeatureError as exc:
            raise ManifestError(str(exc)) from exc

        network = raw.get("network", {})
        if isinstance(network, str):
            network_name, network_external = network, True
        elif isinstance(network, dict):
            network_name = str(network.get("name", "network"))
            network_external = bool(network.get("external", True))
        else:
            raise ManifestError("network must be a string or a mapping")

        port_range = raw.get("port_range", [20000, 20999])
        if not (isinstance(port_range, list) and len(port_range) == 2):
            raise ManifestError("port_range must be a two-element list")
        low, high = int(port_range[0]), int(port_range[1])
        if low >= high:
            raise ManifestError(f"port_range start {low} must be below end {high}")

        variants = raw.get("variants") or ["cpu"]
        if not isinstance(variants, list) or not variants:
            raise ManifestError("variants must be a non-empty list")

        base = dict(DEFAULT_BASES)
        for key, value in (raw.get("base") or {}).items():
            base[str(key)] = str(value)
        for variant in variants:
            if variant not in base:
                raise ManifestError(f"variant {variant!r} has no entry in base:")

        recipes_raw = raw.get("recipes") or {}
        if not isinstance(recipes_raw, dict):
            raise ManifestError("recipes must be a mapping of name -> recipe body")

        recipe_paths = raw.get("recipe_paths") or []
        if isinstance(recipe_paths, str):
            recipe_paths = [recipe_paths]
        if not isinstance(recipe_paths, list):
            raise ManifestError("recipe_paths must be a list of files or directories")

        volumes_raw = raw.get("volumes") or {}
        if not isinstance(volumes_raw, dict):
            raise ManifestError("volumes must be a mapping of name -> path in the container")
        volumes: dict[str, Conditional] = {}
        for volume, target in volumes_raw.items():
            item = Conditional.from_dict(target, where=f"volumes.{volume}", value_key="path")
            if not item.value.startswith("/"):
                raise ManifestError(
                    f"volumes.{volume}: expected an absolute path inside the container, "
                    f"got {item.value!r}"
                )
            volumes[str(volume)] = item

        binds_raw = raw.get("binds") or {}
        if not isinstance(binds_raw, dict):
            raise ManifestError("binds must be a mapping of name -> path in the container")
        binds = {str(name): Bind.from_dict(str(name), item) for name, item in binds_raw.items()}
        _check_bind_paths(binds, volumes)

        labels_raw = raw.get("labels") or {}
        if not isinstance(labels_raw, dict):
            raise ManifestError("labels must be a mapping of label name -> value")
        labels = {
            str(key): Conditional.from_dict(value, where=f"labels.{key}", value_key="value")
            for key, value in labels_raw.items()
        }

        env_raw = raw.get("env") or {}
        if not isinstance(env_raw, dict):
            raise ManifestError("env must be a mapping of key -> rule")
        env = {str(key): EnvRule.from_dict(str(key), rule) for key, rule in env_raw.items()}

        services_raw = raw.get("services") or {}
        if not isinstance(services_raw, dict):
            raise ManifestError("services must be a mapping of slug -> settings")

        manifest = cls(
            name=str(raw.get("name", "bundle")),
            version=version,
            features=features,
            sources=[
                SourceRef.from_dict(source, index=index)
                for index, source in enumerate(raw.get("sources") or [])
            ],
            recipe_paths=[str(item) for item in recipe_paths],
            recipes={str(key): value for key, value in recipes_raw.items()},
            services={
                str(slug): ServiceEntry.from_dict(str(slug), entry)
                for slug, entry in services_raw.items()
            },
            output=str(raw.get("output", "dist")),
            image=str(raw.get("image", "")),
            variants=[str(v) for v in variants],
            base=base,
            network=network_name,
            network_external=network_external,
            volumes=volumes,
            binds=binds,
            labels=labels,
            globals=[str(g) for g in (raw.get("globals") or DEFAULT_GLOBALS)],
            env=env,
            port_range=(low, high),
            path=path,
        )
        manifest.check_features()
        return manifest

    @classmethod
    def load(cls, path: Path) -> Manifest:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ManifestError(f"cannot read {path}: {exc}") from exc
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ManifestError(f"{path} is not valid YAML: {exc}") from exc
        return cls.from_dict(raw, path=path)

    # -- features ---------------------------------------------------------

    def check_features(self) -> None:
        """Refuse a ``when:`` naming a flag that ``features:`` never declares.

        Caught at load time rather than where the condition is evaluated: a mistyped flag
        is simply false everywhere, and the service it guards would vanish from the image
        without a word being said about it.
        """
        used: dict[str, str] = {}
        for source in self.sources:
            for flag in features_mod.names(source.when):
                used.setdefault(flag, f"sources[{source.label}]")
        for slug, entry in self.services.items():
            for flag in features_mod.names(entry.when):
                used.setdefault(flag, f"services.{slug}")
        for key, rule in self.env.items():
            for flag in features_mod.names(rule.when):
                used.setdefault(flag, f"env.{key}")
        for volume, volume_item in self.volumes.items():
            for flag in features_mod.names(volume_item.when):
                used.setdefault(flag, f"volumes.{volume}")
        for bind_name, bind in self.binds.items():
            for flag in features_mod.names(bind.when):
                used.setdefault(flag, f"binds.{bind_name}")
        for label, label_item in self.labels.items():
            for flag in features_mod.names(label_item.when):
                used.setdefault(flag, f"labels.{label}")

        missing = sorted(flag for flag in used if flag not in self.features)
        if missing:
            raise ManifestError(
                "when: names features that are not declared under features:: "
                + ", ".join(f"{flag} (used by {used[flag]})" for flag in missing)
            )

    def holds(self, when: list[str], features: dict[str, bool], *, where: str) -> bool:
        try:
            return features_mod.evaluate(when, features, where=where)
        except features_mod.FeatureError as exc:
            raise ManifestError(str(exc)) from exc

    def active_sources(self, features: dict[str, bool]) -> list[SourceRef]:
        return [
            source
            for source in self.sources
            if self.holds(source.when, features, where=f"sources[{source.label}]")
        ]

    def active_env(self, features: dict[str, bool]) -> dict[str, EnvRule]:
        return {
            key: rule
            for key, rule in self.env.items()
            if self.holds(rule.when, features, where=f"env.{key}")
        }

    def active_volumes(self, features: dict[str, bool]) -> dict[str, str]:
        return {
            name: item.value
            for name, item in self.volumes.items()
            if self.holds(item.when, features, where=f"volumes.{name}")
        }

    def active_binds(self, features: dict[str, bool]) -> dict[str, Bind]:
        return {
            name: bind
            for name, bind in self.binds.items()
            if self.holds(bind.when, features, where=f"binds.{name}")
        }

    def active_labels(self, features: dict[str, bool]) -> dict[str, str]:
        return {
            key: item.value
            for key, item in self.labels.items()
            if self.holds(item.when, features, where=f"labels.{key}")
        }

    def service_mode(
        self, slug: str, features: dict[str, bool], *, default: ServiceMode
    ) -> ServiceMode:
        """The mode a service ends up in, taking ``when:`` and the recipe default in.

        ``default`` is what the recipe asked for: ``sidecar`` for anything that cannot be
        a process inside the bundle, ``bake`` otherwise.
        """
        entry = self.services.get(slug)
        if entry is None:
            return default
        if not self.holds(entry.when, features, where=f"services.{slug}"):
            return ServiceMode.OFF
        return entry.mode if entry.mode is not None else default

    # -- helpers ----------------------------------------------------------

    def output_dir(self) -> Path:
        root = self.path.parent if self.path else Path.cwd()
        return (root / self.output).resolve()

    def resolve(self, relative: str) -> Path:
        """Resolve a path written in the configuration against its own directory."""
        root = self.path.parent if self.path else Path.cwd()
        return (root / relative).resolve()
