"""``bundle.yml`` — the per-project manifest.

The manifest is the single source of truth for generation: the Textual wizard only
edits it, and ``dockerbundle generate --yes`` reads nothing else. It is meant to be
committed, so it pins every decision that would otherwise drift between runs —
notably service slugs and environment-conflict resolutions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.model import MountMode
from core.version import MANIFEST_VERSION

DEFAULT_BASES: dict[str, str] = {
    "cpu": "debian:bookworm-slim",
    "cuda": "nvidia/cuda:12.4.0-base-ubuntu22.04",
}

#: Keys that stay shared across all services instead of being prefixed. These are
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


class ManifestError(ValueError):
    """Raised for a manifest that cannot be acted on."""


@dataclass
class SourceRef:
    """One place to look for candidate services."""

    type: str  # catalog | compose | container | image
    path: str = ""
    ref: str = ""
    name: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        for key in ("path", "ref", "name"):
            value = getattr(self, key)
            if value:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, raw: Any) -> SourceRef:
        if isinstance(raw, str):
            return cls(type="catalog", path=raw)
        if not isinstance(raw, dict):
            raise ManifestError(
                f"source must be a mapping or a path string, got {type(raw).__name__}"
            )
        kind = raw.get("type")
        if kind not in ("catalog", "compose", "container", "image"):
            raise ManifestError(f"unknown source type {kind!r}")
        return cls(
            type=kind,
            path=str(raw.get("path", "")),
            ref=str(raw.get("ref", "")),
            name=str(raw.get("name", "")),
        )


@dataclass
class ServiceEntry:
    """Per-service decisions. The key in ``services:`` is the pinned slug."""

    slug: str
    package: str = ""
    service: str = ""
    enabled: bool = True
    recipe: str = ""
    """Force a specific recipe instead of matching."""

    replicas: int = 1
    ports: dict[int, int] = field(default_factory=dict)
    """Original container port -> port assigned inside the bundle."""

    publish: dict[int, str] = field(default_factory=dict)
    """Original container port -> host publish spec."""

    mounts: dict[str, str] = field(default_factory=dict)
    """Mount target -> ``copy`` | ``volume`` | ``skip``."""

    env_prefix: str = ""
    bakeable: bool | None = None
    """Override the recipe's decision. ``None`` means "use the recipe"."""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.package:
            data["package"] = self.package
        if self.service:
            data["service"] = self.service
        if not self.enabled:
            data["enabled"] = False
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
        if self.bakeable is not None:
            data["bakeable"] = self.bakeable
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
        if not isinstance(replicas, int) or replicas < 1:
            raise ManifestError(f"services.{slug}.replicas must be a positive integer")

        return cls(
            slug=slug,
            package=str(raw.get("package", "")),
            service=str(raw.get("service", "")),
            enabled=bool(raw.get("enabled", True)),
            recipe=str(raw.get("recipe", "")),
            replicas=replicas,
            ports={int(k): int(v) for k, v in (raw.get("ports") or {}).items()},
            publish={int(k): str(v) for k, v in (raw.get("publish") or {}).items()},
            mounts=mounts,
            env_prefix=str(raw.get("env_prefix", "")),
            bakeable=raw.get("bakeable") if isinstance(raw.get("bakeable"), bool) else None,
        )


@dataclass
class Manifest:
    name: str = "bundle"
    version: int = MANIFEST_VERSION
    sources: list[SourceRef] = field(default_factory=list)
    services: dict[str, ServiceEntry] = field(default_factory=dict)
    output: str = "dist"
    variants: list[str] = field(default_factory=lambda: ["cpu"])
    base: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_BASES))
    network: str = "network"
    network_external: bool = True
    globals: list[str] = field(default_factory=lambda: list(DEFAULT_GLOBALS))
    env_conflicts: dict[str, str] = field(default_factory=dict)
    """Env key -> ``prefix`` | ``keep:<slug>`` | ``value:<literal>``."""

    port_range: tuple[int, int] = (20000, 20999)
    path: Path | None = None
    """Where this manifest was loaded from; not serialised."""

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.name,
            "output": self.output,
            "variants": list(self.variants),
            "base": dict(self.base),
            "network": {"name": self.network, "external": self.network_external},
            "port_range": list(self.port_range),
            "globals": list(self.globals),
            "sources": [s.to_dict() for s in self.sources],
            "env_conflicts": dict(sorted(self.env_conflicts.items())),
            "services": {slug: entry.to_dict() for slug, entry in self.services.items()},
        }

    def dump(self) -> str:
        return yaml.safe_dump(
            self.to_dict(), sort_keys=False, allow_unicode=True, default_flow_style=False
        )

    def save(self, path: Path | None = None) -> Path:
        target = path or self.path
        if target is None:
            raise ManifestError("no path to save the manifest to")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.dump(), encoding="utf-8", newline="\n")
        self.path = target
        return target

    # -- deserialisation --------------------------------------------------

    @classmethod
    def from_dict(cls, raw: Any, *, path: Path | None = None) -> Manifest:
        if not isinstance(raw, dict):
            raise ManifestError("bundle.yml must contain a mapping at the top level")

        version = raw.get("version", MANIFEST_VERSION)
        if not isinstance(version, int):
            raise ManifestError("version must be an integer")
        if version > MANIFEST_VERSION:
            raise ManifestError(
                f"bundle.yml declares version {version} but this build only understands "
                f"{MANIFEST_VERSION}; upgrade dockerbundle"
            )

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

        for key, rule in (raw.get("env_conflicts") or {}).items():
            text = str(rule)
            if text != "prefix" and not text.startswith(("keep:", "value:")):
                raise ManifestError(
                    f"env_conflicts.{key}: expected prefix|keep:<slug>|value:<literal>, "
                    f"got {rule!r}"
                )

        services_raw = raw.get("services") or {}
        if not isinstance(services_raw, dict):
            raise ManifestError("services must be a mapping of slug -> settings")

        return cls(
            name=str(raw.get("name", "bundle")),
            version=version,
            sources=[SourceRef.from_dict(s) for s in (raw.get("sources") or [])],
            services={
                str(slug): ServiceEntry.from_dict(str(slug), entry)
                for slug, entry in services_raw.items()
            },
            output=str(raw.get("output", "dist")),
            variants=[str(v) for v in variants],
            base=base,
            network=network_name,
            network_external=network_external,
            globals=[str(g) for g in (raw.get("globals") or DEFAULT_GLOBALS)],
            env_conflicts={str(k): str(v) for k, v in (raw.get("env_conflicts") or {}).items()},
            port_range=(low, high),
            path=path,
        )

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

    # -- helpers ----------------------------------------------------------

    def enabled_services(self) -> list[ServiceEntry]:
        return [entry for entry in self.services.values() if entry.enabled]

    def output_dir(self) -> Path:
        root = self.path.parent if self.path else Path.cwd()
        return (root / self.output).resolve()

    def resolve(self, relative: str) -> Path:
        """Resolve a manifest-relative path against the manifest's directory."""
        root = self.path.parent if self.path else Path.cwd()
        return (root / relative).resolve()
